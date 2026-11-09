"""Flatten an EXPLAIN (ANALYZE, FORMAT JSON) plan into one row per node, with estimate errors.

Pure data transformation: no database or LLM access, so anything can import it.
"""

# Fields that can hold a node's condition; one node may have several (e.g. Index Cond + Filter)
CONDITION_FIELDS = ["Hash Cond", "Merge Cond", "Join Filter",
                    "Index Cond", "Recheck Cond", "Filter", "Cache Key"]

# Below this q-error the estimate counts as accurate (no over/under shown)
ACCURATE_Q = 2

# Nodes that sort, and so can spill to disk when the rows don't fit in work_mem
SORT_OPS = ("Sort", "Incremental Sort")


def q_error(plan_rows, actual_rows):
    # Both counts are at least 1: Actual Rows is a per-loop average rounded to an integer, so it can show 0
    est = max(plan_rows, 1)
    act = max(actual_rows, 1)
    q = max(est / act, act / est)
    if q < ACCURATE_Q:
        direction = "ok"
    elif est > act:
        direction = "over"
    else:
        direction = "under"
    return q, direction


def _sort_info(node):
    """(method, spilled, disk_kb) of a Sort / Incremental Sort, over the leader and every parallel worker.

    The top-level fields describe the leader only; each worker reports its own sort under "Workers", and a
    worker can spill while the leader sorted in memory. disk_kb is the largest spill of any one process.
    Incremental Sort has no top-level method: it reports per group kind ("Full-sort Groups" / "Pre-sorted Groups").
    """
    methods, spilled, disk_kb = [], False, 0
    for report in [node] + node.get("Workers", []):
        if report.get("Sort Method"):
            methods.append(report["Sort Method"])
        if report.get("Sort Space Type") == "Disk":
            spilled = True
            disk_kb = max(disk_kb, report.get("Sort Space Used", 0))
        for group in ("Full-sort Groups", "Pre-sorted Groups"):
            info = report.get(group, {})
            methods += info.get("Sort Methods Used", [])
            if "Sort Space Disk" in info:
                spilled = True
                disk_kb = max(disk_kb, info["Sort Space Disk"].get("Peak Sort Space Used", 0))
    # Prefer the disk method ("external merge" / "external sort"): it's the one that explains the spill
    method = next((m for m in methods if m.startswith("external")), methods[0] if methods else "")
    return method, spilled, disk_kb


def _visit(node, depth, parent_id, rows, processes=1):
    loops = node.get("Actual Loops", 0)
    if loops == 0:
        # Never executed: its row counts mean nothing, so no q-error
        q, direction = None, "not run"
    else:
        q, direction = q_error(node["Plan Rows"], node["Actual Rows"])

    conditions = [f"{field}: {node[field]}" for field in CONDITION_FIELDS if field in node]
    sort_method, sort_spilled, sort_disk_kb = _sort_info(node) if node["Node Type"] in SORT_OPS else ("", False, 0)

    # id = position in the list (parents always come before their children)
    node_id = len(rows)
    rows.append({
        "id": node_id,
        "parent_id": parent_id,
        "depth": depth,
        "op": node["Node Type"],
        "table": node.get("Relation Name", ""),
        "alias": node.get("Alias", ""),
        "plan_rows": node["Plan Rows"],
        "plan_width": node.get("Plan Width", 0),
        "actual_rows": node.get("Actual Rows", 0),
        "actual_total_time": node.get("Actual Total Time", 0),
        "hash_batch": node.get("Hash Batches", 0),
        "original_hash_batch": node.get("Original Hash Batches", 0),
        "sort_method": sort_method,
        "sort_spilled": sort_spilled,
        "sort_disk_kb": sort_disk_kb,
        "loops": loops,
        "processes": processes,
        "q_error": q,
        "direction": direction,
        "condition": " / ".join(conditions),
    })
    if node["Node Type"] in ("Gather", "Gather Merge"):
        child_processes = node.get("Workers Launched", 0) + 1
    else:
        child_processes = processes
    
    for child in node.get("Plans", []):
        _visit(child, depth + 1, node_id, rows, child_processes)


def flatten_plan(explain_output):
    """Return one dict per plan node in depth-first order; [] if there is no plan.

    explain_output is what DBClient.execute_explain returns: [{"Plan": {...}, "Execution Time": ...}].
    The root row has parent_id None.
    """
    if not explain_output:
        return []
    rows = []
    _visit(explain_output[0]["Plan"], 0, None, rows)
    return rows


def format_plan_for_llm(rows):
    """One indented line per node, prefixed with its id, so the LLM can point at nodes by "#id"."""
    lines = []
    for r in rows:
        name = r["op"]
        if r["table"]:
            name += f" on {r['table']}"
            if r["alias"] != r["table"]:
                name += f" {r['alias']}"
        q_text = f"q-error {r['q_error']:.1f} {r['direction']}" if r["q_error"] is not None else "never executed"
        line = (f"{'  ' * r['depth']}#{r['id']} {name} | est {r['plan_rows']} rows, "
                f"actual {r['actual_rows']} rows x {r['loops']} loops, {q_text}")
        if r["sort_spilled"]:
            line += f" | sorted on disk ({r['sort_method']}, {r['sort_disk_kb']} kB)"
        if r["condition"]:
            line += f" | {r['condition']}"
        lines.append(line)
    return "\n".join(lines)
