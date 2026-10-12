"""Tests for the small-table filter in sql_surgeon.agent.nodes. Pure tests — no database or LLM call.

Run from backend/:  ../.venv/bin/python tests/test_small_tables.py
"""
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
from sql_surgeon.agent.nodes import (  # noqa: E402
    SMALL_TABLE_BYTES,
    compute_seq_scan_analysis,
    drop_small_table_indexes,
    drop_small_table_statements,
    find_small_tables,
    index_target_table,
    small_table_warnings,
)

fails = 0


def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    fails += 0 if cond else 1


SMALL = {"kind_type": {"size_bytes": 8192, "est_rows": 7}, "info_type": {"size_bytes": 8192, "est_rows": 113}}

print("=== 1. find_small_tables ===")
meta = {
    "kind_type": {"size_bytes": 8192, "est_rows": 7},
    "title": {"size_bytes": 36008 * 8192, "est_rows": 2528312},
    "edge": {"size_bytes": SMALL_TABLE_BYTES, "est_rows": 100},
    "fresh": {"size_bytes": 0, "est_rows": -1},
    "a_view": {"size_bytes": None, "est_rows": None},
}
small = find_small_tables(meta)
check("a 1-page table is small", "kind_type" in small)
check("a large table is not small", "title" not in small)
check("exactly 64KB does not count (the rule is strictly less than)", "edge" not in small)
check("an empty table is small", "fresh" in small)
check("views / partitioned parents (size is None) do not count", "a_view" not in small)

print("=== 2. index_target_table ===")
check("plain form", index_target_table("CREATE INDEX idx_k ON kind_type(kind);") == "kind_type")
check("no index name", index_target_table("CREATE INDEX ON kind_type (kind)") == "kind_type")
check("IF NOT EXISTS + USING gin", index_target_table(
    "CREATE INDEX IF NOT EXISTS i ON title USING gin (title gin_trgm_ops);") == "title")
check("schema prefix + uppercase", index_target_table("CREATE UNIQUE INDEX i ON public.Kind_Type(kind)") == "kind_type")
check("leading comment", index_target_table("-- Step 1\nCREATE INDEX i ON info_type(info)") == "info_type")
check("an index name containing on is not misread", index_target_table("CREATE INDEX idx_on_title ON title(id)") == "title")
check("not a CREATE INDEX", index_target_table("SELECT * FROM kind_type") is None)

print("=== 3. seq scan verdict ===")
plan = [{"Plan": {"Node Type": "Hash Join", "Plans": [
    {"Node Type": "Seq Scan", "Relation Name": "kind_type", "Actual Rows": 1, "Rows Removed by Filter": 6},
    {"Node Type": "Seq Scan", "Relation Name": "title", "Actual Rows": 100, "Rows Removed by Filter": 99900},
]}}]
verdicts = {r["table"]: r["verdict"] for r in compute_seq_scan_analysis(plan, SMALL)}
check("small table gets small_table (used to be index_likely_helpful)", verdicts["kind_type"] == "small_table")
check("large-table rule is unchanged", verdicts["title"] == "index_likely_helpful")
check("without small tables the behavior is the same as before",
      {r["table"]: r["verdict"] for r in compute_seq_scan_analysis(plan)}["kind_type"] == "index_likely_helpful")

print("=== 4. drop_small_table_indexes ===")
indexes = [
    {"ddl": "CREATE INDEX idx_kt ON kind_type(kind);", "reason": "filter"},
    {"ddl": "CREATE INDEX idx_t ON title(kind_id);", "reason": "join"},
]
kept, skipped = drop_small_table_indexes(indexes, SMALL)
check("only indexes on large tables are kept", [i["ddl"] for i in kept] == ["CREATE INDEX idx_t ON title(kind_id);"])
check("skipped tables are recorded", skipped == [("kind_type", "CREATE INDEX idx_kt ON kind_type(kind);")])
check("None does not raise", drop_small_table_indexes(None, SMALL) == ([], []))

print("=== 5. drop_small_table_statements ===")
script = (
    "-- Step 1: Create indexes (run once)\n"
    "CREATE INDEX idx_kt ON kind_type(kind);\n"
    "CREATE INDEX idx_t ON title(kind_id);\n"
    "CREATE INDEX idx_it ON info_type (info) ;\n"
    "-- Step 2: Run the optimized query\n"
    "SELECT * FROM title t JOIN kind_type kt ON kt.id = t.kind_id WHERE kt.kind = 'a;b';"
)
out, skipped = drop_small_table_statements(script, SMALL)
check("CREATE INDEX on small tables is removed", "kind_type(kind)" not in out and "info_type (info)" not in out)
check("CREATE INDEX on large tables is kept", "CREATE INDEX idx_t ON title(kind_id);" in out)
check("Step comments are kept", "-- Step 1" in out and "-- Step 2" in out)
check("the query itself is unchanged (ON kind_type is not a CREATE INDEX)", out.endswith("WHERE kt.kind = 'a;b';"))
check("both skipped statements are recorded", [t for t, _ in skipped] == ["kind_type", "info_type"])
check("returned unchanged when there are no small tables", drop_small_table_statements(script, {}) == (script, []))

print("=== 6. small_table_warnings ===")
warnings = small_table_warnings(
    [("kind_type", "CREATE INDEX idx_kt ON kind_type(kind);"),
     ("kind_type", "CREATE INDEX  idx_kt ON kind_type(kind)")],  # same index, different spacing / no ';'
    SMALL,
)
check("the same index is warned about only once", len(warnings) == 1)
check("the warning includes table size and row count", "8 kB, ~7 rows" in warnings[0])

print()
print("ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
