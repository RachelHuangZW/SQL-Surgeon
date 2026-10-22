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
check("167x underestimate (the Hash Join in 11d)", round(q, 1) == 166.8 and d == "under")
q, d = q_error(1000, 10)
check("100x overestimate", q == 100 and d == "over")
q, d = q_error(1087137, 869710)
check("off by 1.2x counts as accurate", d == "ok")
q, d = q_error(1, 0)
check("0 actual rows counts as 1, no division by 0", q == 1 and d == "ok")
q, d = q_error(10, 0)
check("estimate 10, actual 0 → 10x overestimate", q == 10 and d == "over")

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
check("one row per node (depth-first)", [r["op"] for r in rows] == ["Hash Join", "Seq Scan", "Hash", "Index Scan", "Seq Scan"])
check("id is the position in the list", [r["id"] for r in rows] == [0, 1, 2, 3, 4])
check("parent_id points to the parent, None for the root", [r["parent_id"] for r in rows] == [None, 0, 0, 2, 0])
check("depth is the tree level", [r["depth"] for r in rows] == [0, 1, 1, 2, 1])
check("JOIN nodes have no table name or alias", rows[0]["table"] == "" and rows[0]["alias"] == "")
check("scan nodes have a table name and alias", rows[1]["table"] == "movie_keyword" and rows[1]["alias"] == "mk")
check("q-error and direction of the root", round(rows[0]["q_error"], 1) == 376.2 and rows[0]["direction"] == "under")
check("single condition", rows[0]["condition"] == "Hash Cond: (mk.keyword_id = k.id)")
check("multiple conditions joined with /", rows[3]["condition"] == "Index Cond: (id = 1) / Filter: (keyword = 'sequel')")
check("empty string when there is no condition", rows[2]["condition"] == "")
check("never-executed node: q-error is None, direction not run",
      rows[4]["q_error"] is None and rows[4]["direction"] == "not run")
check("empty list when there is no plan", flatten_plan(None) == [] and flatten_plan([]) == [])

# processes: below a Gather, every node is run by the launched workers + the leader
gather_plan = [{"Execution Time": 1.0, "Plan": {
    "Node Type": "Gather", "Plan Rows": 1, "Actual Rows": 1, "Actual Loops": 1, "Workers Launched": 2,
    "Plans": [
        {"Node Type": "Hash Join", "Plan Rows": 1, "Actual Rows": 1, "Actual Loops": 3, "Plans": [
            {"Node Type": "Seq Scan", "Plan Rows": 1, "Actual Rows": 1, "Actual Loops": 3},
        ]},
    ],
}}]
g = flatten_plan(gather_plan)
check("Gather itself runs in the leader only → processes 1", g[0]["processes"] == 1)
check("child and grandchild of a Gather with 2 workers → processes 3", [r["processes"] for r in g[1:]] == [3, 3])
check("no Gather in the plan → processes 1 everywhere", [r["processes"] for r in rows] == [1, 1, 1, 1, 1])


print("=== 3. format_plan_for_llm ===")
lines = format_plan_for_llm(rows).splitlines()
check("one line per node", len(lines) == len(rows))
check("root line has #0, row counts and q-error",
      lines[0] == "#0 Hash Join | est 42 rows, actual 15801 rows x 3 loops, q-error 376.2 under | Hash Cond: (mk.keyword_id = k.id)")
check("children are indented by level; alias shown when it differs from the table name", lines[1].startswith("  #1 Seq Scan on movie_keyword mk |"))
check("alias not repeated when it equals the table name", lines[3].startswith("    #3 Index Scan on keyword |"))
check("never-executed node says never executed", lines[4].endswith("never executed"))
check("empty string when there are no nodes", format_plan_for_llm([]) == "")

print()
print("ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
