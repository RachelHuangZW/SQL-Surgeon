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
check("new format: text and node ids are separate", texts == ["a", "b"] and ids == [[12], [2, 3]])

texts, ids = parse_issues(["old style issue"], VALID)
check("old format (plain strings) still works, with empty ids", texts == ["old style issue"] and ids == [[]])

texts, ids = parse_issues([{"issue": "a", "node_ids": [99, 3, "3", True, 3]}], VALID)
check("unknown ids, strings, booleans and duplicate ids are dropped", ids == [[3]])

texts, ids = parse_issues([{"issue": "a"}, {"issue": "b", "node_ids": "3"}], VALID)
check("missing or malformed node_ids gives empty ids", ids == [[], []])

texts, ids = parse_issues([{"issue": ""}, {"node_ids": [1]}, 42, None, "  ", {"issue": "kept", "node_ids": [1]}], VALID)
check("items with empty text, no text or an invalid type are skipped", texts == ["kept"] and ids == [[1]])

check("both lists always have the same length", len(texts) == len(ids))

try:
    parse_issues({"issue": "not a list"}, VALID)
    check("raises ValueError when not an array", False)
except ValueError:
    check("raises ValueError when not an array", True)

print()
print("ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
