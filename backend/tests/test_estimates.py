"""Tests for sql_surgeon.plan.estimates. Pure tests — no database or LLM call.

Run from backend/:  ../.venv/bin/python tests/test_estimates.py
"""
import json
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
from sql_surgeon.plan.table import flatten_plan  # noqa: E402
from sql_surgeon.plan.estimates import (  # noqa: E402
    analyze_estimates, build_parent_to_children_map, find_consequences, find_origins)

fails = 0

def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    fails += 0 if cond else 1

def node(op, est, act, loops=1, time=1.0, plans=(), **extra):
    """造一个 EXPLAIN JSON 节点，只填测试用得到的字段"""
    n = {"Node Type": op, "Plan Rows": est, "Actual Rows": act, "Actual Loops": loops,
         "Actual Total Time": time, "Plans": list(plans)}
    n.update(extra)
    return n

def rows_of(root, execution_time=100.0):
    return flatten_plan([{"Execution Time": execution_time, "Plan": root}])

def origin_ids(root):
    return [o["id"] for o in find_origins(rows_of(root))]

print("=== 1. build_parent_to_children_map ===")
rows = rows_of(node("Hash Join", 10, 10, plans=[
    node("Seq Scan", 10, 10),
    node("Hash", 10, 10, plans=[node("Seq Scan", 10, 10)]),
]))

m = build_parent_to_children_map(rows)
check("父节点 → 子节点 id", {k: [c["id"] for c in v] for k, v in m.items()} == {0: [1, 2], 2: [3]})
check("值是完整的行，不只是 id", m[0][0] is rows[1])
check("叶子节点不在 key 里", 1 not in m and 3 not in m)
check("没有行时是空字典", build_parent_to_children_map([]) == {})

print("=== 2. find_origins ===")
plan_11d = json.load(open(os.path.join(BACKEND, "scripts", "11d_plan.json")))
check("11d 只有 #11 是源头（上面 7 个继承来的错不算）", [o["id"] for o in find_origins(flatten_plan(plan_11d))] == [11])

check("叶子节点估错 → 源头",
      origin_ids(node("Seq Scan", 10, 50000)) == [0])
check("子节点都准、自己错 → 只有自己是源头",
      origin_ids(node("Hash Join", 10, 50000, plans=[node("Seq Scan", 5000, 5000), node("Seq Scan", 20, 20)])) == [0])
check("错误从子节点继承（父子都错 50 倍）→ 只有子节点是源头",
      origin_ids(node("Hash Join", 100, 5000, plans=[node("Seq Scan", 100, 5000), node("Seq Scan", 20, 20)])) == [1])
check("子节点错 50 倍、自己错 5 万倍（又放大 1000 倍）→ 两个都是源头",
      origin_ids(node("Hash Join", 1, 50000, plans=[node("Seq Scan", 100, 5000), node("Seq Scan", 20, 20)])) == [0, 1])
check("放大不到 10 倍（50 → 250）→ 只有子节点是源头",
      origin_ids(node("Hash Join", 20, 5000, plans=[node("Seq Scan", 100, 5000), node("Seq Scan", 20, 20)])) == [1])
check("q-error 低于 10 → 不报", origin_ids(node("Seq Scan", 1000, 9000)) == [])
check("行数太少 → 不报（估 1 行实际 50 行）", origin_ids(node("Seq Scan", 1, 50)) == [])
check("行数按总数算：每次 200 行 × 10 次循环 → 报", origin_ids(node("Seq Scan", 1, 200, loops=10)) == [0])
check("没执行过的节点 → 跳过", origin_ids(node("Seq Scan", 5000, 0, loops=0)) == [])
o = find_origins(rows_of(node("Seq Scan", 10, 50000)))[0]
check("返回 q_error / direction / child_max_q", o["q_error"] == 5000 and o["direction"] == "under" and o["child_max_q"] == 1)

print("=== 3. find_consequences ===")

def hash_join(batches, original, join_time):
    return node("Hash Join", 1000, 3000000, time=join_time, plans=[
        node("Seq Scan", 3000000, 3000000),
        node("Hash", 1000, 3000000, plans=[node("Seq Scan", 1000, 3000000)],
             **{"Hash Batches": batches, "Original Hash Batches": original}),
    ])
    
c = find_consequences(rows_of(hash_join(32, 1, 60)), 100.0)
check("Hash 实际 32 批 > 计划 1 批，Hash Join 占 60% → hash_spill",
      len(c) == 1 and c[0]["type"] == "hash_spill" and c[0]["id"] == 2
      and c[0]["planned_batches"] == 1 and c[0]["actual_batches"] == 32 and round(c[0]["time_share"], 2) == 0.6)
check("计划就是 8 批、实际 8 批 → 不报", find_consequences(rows_of(hash_join(8, 8, 60)), 100.0) == [])
check("溢出了但 Hash Join 只占 5% → 不报", find_consequences(rows_of(hash_join(32, 1, 5)), 100.0) == [])

def nested_loop(inner_loops, inner_time):
    return node("Nested Loop", 10, inner_loops, time=95, plans=[
        node("Seq Scan", 10, inner_loops),
        node("Index Scan", 1, 1, loops=inner_loops, time=inner_time),
    ])


c = find_consequences(rows_of(nested_loop(100000, 0.0003)), 100.0)
check("内侧计划 10 次、实际 10 万次，占 30% → nested_loop_rescan",
      len(c) == 1 and c[0]["type"] == "nested_loop_rescan" and c[0]["id"] == 2
      and c[0]["planned_loops"] == 10 and c[0]["actual_loops"] == 100000 and round(c[0]["time_share"], 2) == 0.3)
check("内侧多跑了很多次但只占 1% → 不报", find_consequences(rows_of(nested_loop(100000, 0.00001)), 100.0) == [])
check("内侧只比计划多 5 倍 → 不报", find_consequences(rows_of(nested_loop(50, 1.0)), 100.0) == [])
check("没有执行时间 → 空列表", find_consequences(rows_of(nested_loop(100000, 0.0003)), None) == [])
check("11d 没有后果（391ms 里没有哪个节点到 10%）", find_consequences(flatten_plan(plan_11d), 391.16) == [])

print("=== 4. analyze_estimates ===")
r = analyze_estimates(plan_11d)
check("11d：返回 origins 和 consequences", [o["id"] for o in r["origins"]] == [11] and r["consequences"] == [])
check("没有计划时两个都是空列表",
      analyze_estimates(None) == {"origins": [], "consequences": []}
      and analyze_estimates([]) == {"origins": [], "consequences": []})

print()
print("ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
