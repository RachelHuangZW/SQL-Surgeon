"""Flatten an EXPLAIN (ANALYZE, FORMAT JSON) plan into one row per node, with estimate errors.

Pure data transformation: no database or LLM access, so anything can import it.
"""

# Fields that can hold a node's condition; one node may have several (e.g. Index Cond + Filter)
CONDITION_FIELDS = ["Hash Cond", "Merge Cond", "Join Filter",
                    "Index Cond", "Recheck Cond", "Filter", "Cache Key"]

# Below this q-error the estimate counts as accurate (no over/under shown)
ACCURATE_Q = 2


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


def _visit(node, depth, parent_id, rows):
    loops = node.get("Actual Loops", 0)
    if loops == 0:
        # Never executed: its row counts mean nothing, so no q-error
        q, direction = None, "not run"
    else:
        q, direction = q_error(node["Plan Rows"], node["Actual Rows"])

    conditions = [f"{field}: {node[field]}" for field in CONDITION_FIELDS if field in node]

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
        "actual_rows": node.get("Actual Rows", 0),
        "loops": loops,
        "q_error": q,
        "direction": direction,
        "condition": " / ".join(conditions),
    })
    for child in node.get("Plans", []):
        _visit(child, depth + 1, node_id, rows)


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
        if r["condition"]:
            line += f" | {r['condition']}"
        lines.append(line)
    return "\n".join(lines)
