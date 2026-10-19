from .table import ACCURATE_Q, flatten_plan

# q-error
BAD_Q = 10
# 
SECOND_SOURCE_RATIO = 10
# share of total execution time
TIME_SHARE = 0.10
# too few rows: don't report
MIN_ROWS = 1000
# Nodes that read their whole input before returning the first row
FULL_READ_OPS = {"Sort", "Hash"}
# Parents that can stop pulling rows from a child before the child is exhausted
EARLY_STOP_OPS = {"Merge Join", "Limit"}

def build_parent_to_children_map(rows):
    """
    Walk the rows from flatten_plan and build a {parent_id: [child rows]} dict
    Leaf nodes are not keys, so look up with .get(id, [])
    """
    parent_map = {}
    for row in rows:
        # The root has no parent, skip it
        if row["parent_id"] is None:
            continue
        # First time we see this parent: create an empty list, then add the current row
        parent_map.setdefault(row["parent_id"], []).append(row)
    return parent_map


def find_partial_reads(rows, children_map):
    """ids of nodes whose Actual Rows may count only part of their output"""
    partial = set()
    
    def mark(node):
        partial.add(node["id"])
        # Its own output may be cut short, but it already read everything below it
        if node["op"] in FULL_READ_OPS:
            return
        for child in children_map.get(node["id"], []):
            mark(child)
    
    for row in rows:
        if row["op"] in EARLY_STOP_OPS:
            for child in children_map.get(row["id"], []):
                mark(child)
    
    return partial


def find_origins(rows):
    """
    Find where misestimates originate: the node is badly wrong, and the error is not inherited from its children
    Returns [{"id", "q_error", "direction", "child_max_q"}]
    """
    children_map = build_parent_to_children_map(rows)
    origins = []
    partial = find_partial_reads(rows, children_map)
    
    def trustworthy(row):
        # A partial read can only make Actual Rows smaller, so only an overestimate can be fake
        return not (row["id"] in partial and row["direction"] == "over")

    for row in rows:
        # Never-executed nodes have no q-error
        if row["q_error"] is None:
            continue
        if not trustworthy(row):
            continue
        # Not wrong enough
        if row["q_error"] < BAD_Q:
            continue
        # Too few rows: the larger of estimated and actual, times loops = total rows
        if max(row["plan_rows"], row["actual_rows"]) * row["loops"] < MIN_ROWS:
            continue
        
        # Largest q-error among the children; a leaf has no children, so treat it as 1 (exact)
        child_qs = [c["q_error"] for c in children_map.get(row["id"], []) if c["q_error"] is not None and trustworthy(c)]
        child_max_q = max(child_qs, default=1)

        # Rule 1: all children are accurate → the error first appears here
        # Rule 2: a child is already wrong, but this node is SECOND_SOURCE_RATIO times worse → a second error source
        if child_max_q < ACCURATE_Q or row["q_error"] >= child_max_q * SECOND_SOURCE_RATIO:
            origins.append({
                "id": row["id"],
                "q_error": row["q_error"],
                "direction": row["direction"],
                "child_max_q": child_max_q
            })
    
    return origins


def find_consequences(rows, execution_time_ms):
    """
    Find slow operations caused by misestimates; only report those taking >= TIME_SHARE of total execution time
    Returns [{"id", "type", "time_share", ...}]
    """
    if not execution_time_ms:
        return []
    children_map = build_parent_to_children_map(rows)

    def time_share(row):
        # Actual Total Time is a per-loop average and includes the children's time
        # Note: in parallel plans loops includes the workers, so this overcounts and leans toward reporting more
        return row["actual_total_time"] * row["loops"] / execution_time_ms

    consequences = []
    for row in rows:
        # Consequence 1: Hash used more batches than planned → rows underestimated, didn't fit in memory, spilled to disk
        if row["op"] == "Hash" and row["hash_batch"] > row["original_hash_batch"]:
            # The spill cost mostly shows up in the parent Hash Join
            join = next(r for r in rows if r["id"] == row["parent_id"])
            share = time_share(join)
            if share >= TIME_SHARE:
                consequences.append({
                    "id": row["id"],
                    "type": "hash_spill",
                    "time_share": share,
                    "planned_batches": row["original_hash_batch"],
                    "actual_batches": row["hash_batch"],
                })

        # Consequence 2: the Nested Loop inner side ran far more times than planned
        if row["op"] == "Nested Loop":
            children = children_map.get(row["id"], [])
            if len(children) != 2:
                continue
            # The first child is the outer side, the second is the inner side
            outer, inner = children
            # Planned inner executions = outer estimated rows × outer loops
            planned_loops = max(outer["plan_rows"] * outer["loops"], 1)
            share = time_share(inner)
            if inner["loops"] / planned_loops >= BAD_Q and share >= TIME_SHARE:
                consequences.append({
                    "id": inner["id"],
                    "type": "nested_loop_rescan",
                    "time_share": share,
                    "planned_loops": planned_loops,
                    "actual_loops": inner["loops"],
                })
    return consequences


def analyze_estimates(explain_output):
    """
    Entry point: takes the result of DBClient.execute_explain, returns {"origins": [...], "consequences": [...]}
    """
    rows = flatten_plan(explain_output)
    if not rows:
        return {"origins": [], "consequences": []}
    execution_time_ms = explain_output[0].get("Execution Time")
    return {
        "origins": find_origins(rows),
        "consequences": find_consequences(rows, execution_time_ms),
    }
    
    
def format_estimates_for_llm(analysis, rows):
    """One line per origin / consequence, pointing at plan nodes by "#id" like format_plan_for_llm."""
    if not analysis:
        return "None"
    by_id = {r["id"]: r for r in rows}
    lines = []
    for o in analysis["origins"]:
        r = by_id[o["id"]]
        line = (f"Origin #{r['id']} {r['op']}: est {r['plan_rows']} rows, actual {r['actual_rows']} rows per loop, "
                f"q-error {o['q_error']:.0f} {o['direction']}; ")
        # Same two cases as the rules in find_origins
        if o["child_max_q"] < ACCURATE_Q:
            line += "its inputs are accurate, so the error starts here"
        else:
            line += (f"its inputs are off by up to {o['child_max_q']:.0f}x and this node makes it "
                     f"{o['q_error'] / o['child_max_q']:.0f}x worse")
        lines.append(line)
    for c in analysis["consequences"]:
        r = by_id[c["id"]]
        # :.0% turns 0.3 into "30%"
        share = f"{c['time_share']:.0%} of execution time"
        if c["type"] == "hash_spill":
            lines.append(f"Consequence #{r['id']} {r['op']}: hash spilled to disk, batches planned "
                         f"{c['planned_batches']}, used {c['actual_batches']}; its join takes {share}")
        else:
            lines.append(f"Consequence #{r['id']} {r['op']}: Nested Loop inner side ran {c['actual_loops']} times, "
                         f"planned {c['planned_loops']}; takes {share}")
    # An empty list joins to "", which is falsy, so "found nothing" also becomes "None"
    return "\n".join(lines) or "None"

    