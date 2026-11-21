"""Tests for sql_surgeon/plan/benchmark.py (#16). No database needed.

Run from backend/:  ../.venv/bin/python tests/test_benchmark_summary.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sql_surgeon.plan.benchmark import benchmark_summary, plan_index_names  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    fails += 0 if cond else 1


seq = [{"Execution Time": 40.0, "Plan": {"Node Type": "Seq Scan", "Relation Name": "title",
                                         "Shared Hit Blocks": 500, "Shared Read Blocks": 20}}]
indexed = [{"Execution Time": 2.0, "Plan": {
    "Node Type": "Bitmap Heap Scan", "Shared Hit Blocks": 12, "Shared Read Blocks": 0,
    "Plans": [{"Node Type": "Bitmap Index Scan", "Index Name": "title_production_year_idx",
               "Shared Hit Blocks": 3}],
}}]

print("=== 1. plan_index_names ===")
check("an index below the top node is found", plan_index_names(indexed) == {"title_production_year_idx"})
check("no index in a seq scan plan", plan_index_names(seq) == set())
check("empty plan", plan_index_names([]) == set())

print("\n=== 2. benchmark_summary ===")
summary = benchmark_summary({
    "before": seq,
    "after": indexed,
    "indexes": [{"ddl": "CREATE INDEX ON title (production_year)", "name": "title_production_year_idx"},
                {"ddl": "CREATE INDEX ON title (kind_id)", "name": "title_kind_id_idx"},
                {"ddl": "CREATE INDEX IF NOT EXISTS i ON title (kind_id)", "name": None}],
    "skipped": [],
})
check("before: time and the top node's buffers", summary["before"] == {"execution_ms": 40.0, "shared_hit": 500, "shared_read": 20})
check("after: totals come from the top node only, not summed with children",
      summary["after"] == {"execution_ms": 2.0, "shared_hit": 12, "shared_read": 0})
check("used / not used per index", [i["used"] for i in summary["indexes"]] == [True, False, False])
check("ddl and name are kept", summary["indexes"][0]["ddl"] == "CREATE INDEX ON title (production_year)"
      and summary["indexes"][2]["name"] is None)
no_buffers = benchmark_summary({"before": [{"Execution Time": 1.0, "Plan": {"Node Type": "Result"}}],
                                "after": [{"Execution Time": 1.0, "Plan": {"Node Type": "Result"}}],
                                "indexes": [], "skipped": []})
check("missing buffer fields count as 0", no_buffers["before"]["shared_hit"] == 0 and no_buffers["indexes"] == [])

print(f"\n{fails} failed")
sys.exit(1 if fails else 0)
