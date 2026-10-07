"""Tests for parse_issues in sql_surgeon.agent.nodes. Pure tests — no database or LLM call.

Run from backend/:  ../.venv/bin/python tests/test_issue_parsing.py
"""
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
from sql_surgeon.agent.nodes import parse_issues  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    fails += 0 if cond else 1


VALID = {0, 1, 2, 3, 12}

print("=== parse_issues ===")
texts, ids = parse_issues([{"issue": "a", "node_ids": [12]}, {"issue": "b", "node_ids": [2, 3]}], VALID)
check("新格式：文字和节点 id 分开", texts == ["a", "b"] and ids == [[12], [2, 3]])

texts, ids = parse_issues(["old style issue"], VALID)
check("旧格式（纯字符串）也能用，id 为空", texts == ["old style issue"] and ids == [[]])

texts, ids = parse_issues([{"issue": "a", "node_ids": [99, 3, "3", True, 3]}], VALID)
check("不存在的 id、字符串、布尔值、重复的 id 都被去掉", ids == [[3]])

texts, ids = parse_issues([{"issue": "a"}, {"issue": "b", "node_ids": "3"}], VALID)
check("缺少 node_ids 或格式不对时，id 为空", ids == [[], []])

texts, ids = parse_issues([{"issue": ""}, {"node_ids": [1]}, 42, None, "  ", {"issue": "kept", "node_ids": [1]}], VALID)
check("空文字、没有文字、非法类型的项被跳过", texts == ["kept"] and ids == [[1]])

check("两个列表长度永远一样", len(texts) == len(ids))

try:
    parse_issues({"issue": "not a list"}, VALID)
    check("不是数组时报 ValueError", False)
except ValueError:
    check("不是数组时报 ValueError", True)

print()
print("ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
