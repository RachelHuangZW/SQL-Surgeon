"""Before/after figures of a sandbox benchmark (db.client.DBClient.benchmark_in_sandbox)."""


def plan_index_names(plan) -> set:
    """Names of the indexes a plan scans (Index Scan, Index Only Scan, Bitmap Index Scan)."""
    if not plan:
        return set()
    names = set()

    def visit(node):
        if "Index Name" in node:
            names.add(node["Index Name"])
        for child in node.get("Plans", []):
            visit(child)

    visit(plan[0]["Plan"])
    return names


def _totals(plan) -> dict:
    # A node's buffer counts include its children's, so the top node's are the whole execution's
    top = plan[0]["Plan"]
    return {
        "execution_ms": plan[0]["Execution Time"],
        "shared_hit": top.get("Shared Hit Blocks", 0),
        "shared_read": top.get("Shared Read Blocks", 0),
    }


def benchmark_summary(benchmark: dict) -> dict:
    """{"before": totals, "after": totals, "indexes": [{"ddl", "name", "used"}]} for the frontend."""
    used = plan_index_names(benchmark["after"])
    return {
        "before": _totals(benchmark["before"]),
        "after": _totals(benchmark["after"]),
        "indexes": [{**index, "used": index["name"] in used} for index in benchmark["indexes"]],
    }
