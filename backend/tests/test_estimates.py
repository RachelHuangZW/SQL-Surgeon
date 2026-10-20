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
    analyze_estimates, build_parent_to_children_map, find_consequences, find_origins, find_partial_reads)

fails = 0

def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    fails += 0 if cond else 1

def node(op, est, act, loops=1, time=1.0, plans=(), **extra):
    """Build an EXPLAIN JSON node with only the fields these tests use"""
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
check("parent → child ids", {k: [c["id"] for c in v] for k, v in m.items()} == {0: [1, 2], 2: [3]})
check("values are full rows, not just ids", m[0][0] is rows[1])
check("leaf nodes are not keys", 1 not in m and 3 not in m)
check("empty dict when there are no rows", build_parent_to_children_map([]) == {})

print("=== 2. find_origins ===")
plan_11d = json.load(open(os.path.join(BACKEND, "scripts", "11d_plan.json")))
check("11d: only #11 is an origin (the 7 inherited errors above it do not count)", [o["id"] for o in find_origins(flatten_plan(plan_11d))] == [11])

check("misestimated leaf → origin",
      origin_ids(node("Seq Scan", 10, 50000)) == [0])
check("children accurate, node wrong → only the node is an origin",
      origin_ids(node("Hash Join", 10, 50000, plans=[node("Seq Scan", 5000, 5000), node("Seq Scan", 20, 20)])) == [0])
check("error inherited from a child (both off by 50x) → only the child is an origin",
      origin_ids(node("Hash Join", 100, 5000, plans=[node("Seq Scan", 100, 5000), node("Seq Scan", 20, 20)])) == [1])
check("child off by 50x, node off by 50,000x (another 1000x) → both are origins",
      origin_ids(node("Hash Join", 1, 50000, plans=[node("Seq Scan", 100, 5000), node("Seq Scan", 20, 20)])) == [0, 1])
check("amplified by less than 10x (50 → 250) → only the child is an origin",
      origin_ids(node("Hash Join", 20, 5000, plans=[node("Seq Scan", 100, 5000), node("Seq Scan", 20, 20)])) == [1])
check("q-error below 10 → not reported", origin_ids(node("Seq Scan", 1000, 9000)) == [])
check("too few rows → not reported (estimate 1, actual 50)", origin_ids(node("Seq Scan", 1, 50)) == [])
check("rows are counted in total: 200 per loop × 10 loops → reported", origin_ids(node("Seq Scan", 1, 200, loops=10)) == [0])
check("never-executed node → skipped", origin_ids(node("Seq Scan", 5000, 0, loops=0)) == [])
o = find_origins(rows_of(node("Seq Scan", 10, 50000)))[0]
check("returns q_error / direction / child_max_q", o["q_error"] == 5000 and o["direction"] == "under" and o["child_max_q"] == 1)

print("=== 3. find_consequences ===")

def hash_join(batches, original, join_time):
    return node("Hash Join", 1000, 3000000, time=join_time, plans=[
        node("Seq Scan", 3000000, 3000000),
        node("Hash", 1000, 3000000, plans=[node("Seq Scan", 1000, 3000000)],
             **{"Hash Batches": batches, "Original Hash Batches": original}),
    ])
    
c = find_consequences(rows_of(hash_join(32, 1, 60)), 100.0)
check("Hash used 32 batches > 1 planned, Hash Join takes 60% → hash_spill",
      len(c) == 1 and c[0]["type"] == "hash_spill" and c[0]["id"] == 2
      and c[0]["planned_batches"] == 1 and c[0]["actual_batches"] == 32 and round(c[0]["time_share"], 2) == 0.6)
check("8 batches planned, 8 used → not reported", find_consequences(rows_of(hash_join(8, 8, 60)), 100.0) == [])
check("spilled but Hash Join takes only 5% → not reported", find_consequences(rows_of(hash_join(32, 1, 5)), 100.0) == [])

def nested_loop(inner_loops, inner_time):
    return node("Nested Loop", 10, inner_loops, time=95, plans=[
        node("Seq Scan", 10, inner_loops),
        node("Index Scan", 1, 1, loops=inner_loops, time=inner_time),
    ])


c = find_consequences(rows_of(nested_loop(100000, 0.0003)), 100.0)
check("inner side planned 10 times, ran 100,000 times, takes 30% → nested_loop_rescan",
      len(c) == 1 and c[0]["type"] == "nested_loop_rescan" and c[0]["id"] == 2
      and c[0]["planned_loops"] == 10 and c[0]["actual_loops"] == 100000 and round(c[0]["time_share"], 2) == 0.3)
check("inner side ran many extra times but takes only 1% → not reported", find_consequences(rows_of(nested_loop(100000, 0.00001)), 100.0) == [])
check("inner side ran only 5x more than planned → not reported", find_consequences(rows_of(nested_loop(50, 1.0)), 100.0) == [])
check("no execution time → empty list", find_consequences(rows_of(nested_loop(100000, 0.0003)), None) == [])
check("11d has no consequences (no node reaches 10% of 391ms)", find_consequences(flatten_plan(plan_11d), 391.16) == [])

def nested_loop_memoize(lookups, misses):
    """Nested Loop whose inner side is a Memoize: `lookups` cache lookups, `misses` real executions below it"""
    return node("Nested Loop", 10, lookups, time=95, plans=[
        node("Seq Scan", 10, lookups),                                   # #1 outer: planned 10 rows, produced `lookups`
        node("Memoize", 1, 1, loops=lookups, time=0.0003, plans=[        # #2 30% of the time, so only the loop count decides
            node("Index Scan", 1, 1, loops=misses, time=0.001),          # #3 runs once per cache miss
        ]),
    ])

c = find_consequences(rows_of(nested_loop_memoize(100000, 5)), 100.0)
check("Memoize: 100,000 lookups but only 5 real executions (planned 10) → not reported", c == [])

c = find_consequences(rows_of(nested_loop_memoize(100000, 50000)), 100.0)
check("Memoize: 50,000 real executions, planned 10 → reported with the real count",
      len(c) == 1 and c[0]["type"] == "nested_loop_rescan" and c[0]["actual_loops"] == 50000)

print("=== 4. analyze_estimates ===")
r = analyze_estimates(plan_11d)
check("11d: returns origins and consequences", [o["id"] for o in r["origins"]] == [11] and r["consequences"] == [])
check("both lists empty when there is no plan",
      analyze_estimates(None) == {"origins": [], "consequences": []}
      and analyze_estimates([]) == {"origins": [], "consequences": []})

print("=== 5. partial reads under Merge Join / Limit ===")
plan_11d_inlined = json.load(open(os.path.join(BACKEND, "scripts", "11d_inlined_plan.json")))


def partial_ids(root):
    rows = rows_of(root)
    return find_partial_reads(rows, build_parent_to_children_map(rows))


# Merge Join (#0) over 11.5x; its left Sort (#1) was cut short at 161 of 18,219 rows; right Sort (#3) is exact
merge = node("Merge Join", 5620, 487, plans=[
    node("Sort", 18219, 161, plans=[node("Seq Scan", 18219, 15801)]),
    node("Sort", 29997, 30012, plans=[node("Seq Scan", 29997, 29997)]),
])
check("both children of a Merge Join may be partial; a Sort stops the marking", partial_ids(merge) == {1, 3})
check("cut-short Sort under a Merge Join is not an origin, the Merge Join itself is", origin_ids(merge) == [0])
check("an underestimate under a Merge Join is still an origin (a partial read only lowers Actual Rows)",
      origin_ids(node("Merge Join", 10, 10, plans=[
          node("Sort", 10, 5000, plans=[node("Seq Scan", 5000, 5000)]),
          node("Sort", 10, 10, plans=[node("Seq Scan", 10, 10)]),
      ])) == [1])
check("a real overestimate below the Sort is still an origin (the Sort read all of it)",
      origin_ids(node("Merge Join", 100, 100, plans=[
          node("Sort", 100, 100, plans=[node("Seq Scan", 5000, 100)]),
          node("Sort", 100, 100, plans=[node("Seq Scan", 100, 100)]),
      ])) == [2])
check("without a Sort, the marking goes all the way down",
      partial_ids(node("Merge Join", 10, 10, plans=[
          node("Nested Loop", 10, 10, plans=[node("Seq Scan", 10, 10), node("Index Scan", 1, 1)]),
          node("Index Scan", 10, 10),
      ])) == {1, 2, 3, 4})
check("an overestimate under a Limit is not an origin",
      origin_ids(node("Limit", 10, 10, plans=[node("Index Scan", 50000, 10)])) == [])
check("11d with inlined keyword ids: origin is #7 Merge Join, not the cut-short #8 Sort",
      [o["id"] for o in find_origins(flatten_plan(plan_11d_inlined))] == [7])

print()
print("ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
