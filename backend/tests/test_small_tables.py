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
check("1 页的表是小表", "kind_type" in small)
check("大表不是小表", "title" not in small)
check("正好 64KB 不算（标准是小于）", "edge" not in small)
check("空表算小表", "fresh" in small)
check("视图 / 分区父表（size 为 None）不算", "a_view" not in small)

print("=== 2. index_target_table ===")
check("普通写法", index_target_table("CREATE INDEX idx_k ON kind_type(kind);") == "kind_type")
check("没有索引名", index_target_table("CREATE INDEX ON kind_type (kind)") == "kind_type")
check("IF NOT EXISTS + USING gin", index_target_table(
    "CREATE INDEX IF NOT EXISTS i ON title USING gin (title gin_trgm_ops);") == "title")
check("schema 前缀 + 大写", index_target_table("CREATE UNIQUE INDEX i ON public.Kind_Type(kind)") == "kind_type")
check("前面有注释", index_target_table("-- Step 1\nCREATE INDEX i ON info_type(info)") == "info_type")
check("索引名里带 on 不误判", index_target_table("CREATE INDEX idx_on_title ON title(id)") == "title")
check("不是 CREATE INDEX", index_target_table("SELECT * FROM kind_type") is None)

print("=== 3. seq scan verdict ===")
plan = [{"Plan": {"Node Type": "Hash Join", "Plans": [
    {"Node Type": "Seq Scan", "Relation Name": "kind_type", "Actual Rows": 1, "Rows Removed by Filter": 6},
    {"Node Type": "Seq Scan", "Relation Name": "title", "Actual Rows": 100, "Rows Removed by Filter": 99900},
]}}]
verdicts = {r["table"]: r["verdict"] for r in compute_seq_scan_analysis(plan, SMALL)}
check("小表判定为 small_table（以前是 index_likely_helpful）", verdicts["kind_type"] == "small_table")
check("大表规则不变", verdicts["title"] == "index_likely_helpful")
check("不传小表时行为和以前一样",
      {r["table"]: r["verdict"] for r in compute_seq_scan_analysis(plan)}["kind_type"] == "index_likely_helpful")

print("=== 4. drop_small_table_indexes ===")
indexes = [
    {"ddl": "CREATE INDEX idx_kt ON kind_type(kind);", "reason": "filter"},
    {"ddl": "CREATE INDEX idx_t ON title(kind_id);", "reason": "join"},
]
kept, skipped = drop_small_table_indexes(indexes, SMALL)
check("只留下大表的索引", [i["ddl"] for i in kept] == ["CREATE INDEX idx_t ON title(kind_id);"])
check("记录被跳过的表", skipped == [("kind_type", "CREATE INDEX idx_kt ON kind_type(kind);")])
check("None 不报错", drop_small_table_indexes(None, SMALL) == ([], []))

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
check("小表的 CREATE INDEX 被删掉", "kind_type(kind)" not in out and "info_type (info)" not in out)
check("大表的 CREATE INDEX 还在", "CREATE INDEX idx_t ON title(kind_id);" in out)
check("Step 注释还在", "-- Step 1" in out and "-- Step 2" in out)
check("查询本身原样保留（ON kind_type 不算 CREATE INDEX）", out.endswith("WHERE kt.kind = 'a;b';"))
check("记录两条被跳过的语句", [t for t, _ in skipped] == ["kind_type", "info_type"])
check("没有小表时原样返回", drop_small_table_statements(script, {}) == (script, []))

print("=== 6. small_table_warnings ===")
warnings = small_table_warnings(
    [("kind_type", "CREATE INDEX idx_kt ON kind_type(kind);"),
     ("kind_type", "CREATE INDEX  idx_kt ON kind_type(kind)")],  # same index, different spacing / no ';'
    SMALL,
)
check("同一个索引只提示一次", len(warnings) == 1)
check("提示里有表大小和行数", "8 kB, ~7 rows" in warnings[0])

print()
print("ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
