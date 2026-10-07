"""Tests for sql_surgeon.plan.table. Pure tests — no database or LLM call.

Run from backend/:  ../.venv/bin/python tests/test_plan_table.py
"""
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
from sql_surgeon.plan.table import flatten_plan, format_plan_for_llm, q_error  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    fails += 0 if cond else 1


print("=== 1. q_error ===")
q, d = q_error(31, 5170)
check("低估 167 倍（11d 的 Hash Join）", round(q, 1) == 166.8 and d == "under")
q, d = q_error(1000, 10)
check("高估 100 倍", q == 100 and d == "over")
q, d = q_error(1087137, 869710)
check("差 1.2 倍算准", d == "ok")
q, d = q_error(1, 0)
check("实际 0 行按 1 算，不会除以 0", q == 1 and d == "ok")
q, d = q_error(10, 0)
check("估算 10、实际 0 → 高估 10 倍", q == 10 and d == "over")

print("=== 2. flatten_plan ===")
plan = [{"Execution Time": 12.3, "Plan": {
    "Node Type": "Hash Join", "Plan Rows": 42, "Actual Rows": 15801, "Actual Loops": 3,
    "Hash Cond": "(mk.keyword_id = k.id)",
    "Plans": [
        {"Node Type": "Seq Scan", "Relation Name": "movie_keyword", "Alias": "mk",
         "Plan Rows": 1884688, "Actual Rows": 1507977, "Actual Loops": 3},
        {"Node Type": "Hash", "Plan Rows": 3, "Actual Rows": 3, "Actual Loops": 3, "Plans": [
            {"Node Type": "Index Scan", "Relation Name": "keyword", "Alias": "keyword",
             "Plan Rows": 3, "Actual Rows": 3, "Actual Loops": 3,
             "Index Cond": "(id = 1)", "Filter": "(keyword = 'sequel')"},
        ]},
        {"Node Type": "Seq Scan", "Relation Name": "title", "Alias": "t",
         "Plan Rows": 100, "Actual Rows": 0, "Actual Loops": 0},
    ],
}}]
rows = flatten_plan(plan)
check("每个节点一行（深度优先）", [r["op"] for r in rows] == ["Hash Join", "Seq Scan", "Hash", "Index Scan", "Seq Scan"])
check("id 就是列表里的位置", [r["id"] for r in rows] == [0, 1, 2, 3, 4])
check("parent_id 指向父节点，根节点是 None", [r["parent_id"] for r in rows] == [None, 0, 0, 2, 0])
check("depth 是层级", [r["depth"] for r in rows] == [0, 1, 1, 2, 1])
check("JOIN 节点没有表名和别名", rows[0]["table"] == "" and rows[0]["alias"] == "")
check("扫描节点有表名和别名", rows[1]["table"] == "movie_keyword" and rows[1]["alias"] == "mk")
check("根节点的 q-error 和方向", round(rows[0]["q_error"], 1) == 376.2 and rows[0]["direction"] == "under")
check("单个条件", rows[0]["condition"] == "Hash Cond: (mk.keyword_id = k.id)")
check("多个条件用 / 连接", rows[3]["condition"] == "Index Cond: (id = 1) / Filter: (keyword = 'sequel')")
check("没有条件时是空字符串", rows[2]["condition"] == "")
check("未执行的节点：q-error 为 None，方向 not run",
      rows[4]["q_error"] is None and rows[4]["direction"] == "not run")
check("没有计划时返回空列表", flatten_plan(None) == [] and flatten_plan([]) == [])

print("=== 3. format_plan_for_llm ===")
lines = format_plan_for_llm(rows).splitlines()
check("每个节点一行", len(lines) == len(rows))
check("根节点带 #0、行数和 q-error",
      lines[0] == "#0 Hash Join | est 42 rows, actual 15801 rows x 3 loops, q-error 376.2 under | Hash Cond: (mk.keyword_id = k.id)")
check("子节点按层级缩进，别名和表名不同时显示别名", lines[1].startswith("  #1 Seq Scan on movie_keyword mk |"))
check("别名和表名相同时不重复显示", lines[3].startswith("    #3 Index Scan on keyword |"))
check("未执行的节点写 never executed", lines[4].endswith("never executed"))
check("没有节点时是空字符串", format_plan_for_llm([]) == "")

print()
print("ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
