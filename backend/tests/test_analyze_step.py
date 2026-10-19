"""Tests for the "Step 3: ANALYZE" line added to the advice script. Pure tests — no database or LLM call.

Run from backend/:  ../.venv/bin/python tests/test_analyze_step.py
"""
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
from sql_surgeon.agent.nodes import _build_analyze_statement, is_expression_index  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    fails += 0 if cond else 1


print("=== 1. is_expression_index ===")
check("plain column", not is_expression_index("CREATE INDEX i ON t(a);"))
check("several columns, DESC and opclass", not is_expression_index("CREATE INDEX i ON t (a DESC, b text_pattern_ops)"))
check("USING + schema prefix", not is_expression_index("CREATE INDEX i ON public.t USING btree (a)"))
check("INCLUDE and WHERE after the key list don't count",
      not is_expression_index("CREATE INDEX i ON t (a) INCLUDE (b) WHERE (c > 1)"))
check("function call", is_expression_index("CREATE INDEX i ON users (lower(email))"))
check("parenthesized expression", is_expression_index("CREATE INDEX i ON t ((a + b))"))
check("expression after a plain column", is_expression_index("CREATE INDEX i ON t (a, lower(b))"))
check("leading comment", is_expression_index("-- Step 1\nCREATE INDEX i ON t (lower(b))"))
check("not a CREATE INDEX", not is_expression_index("SELECT lower(a) FROM t"))

print("=== 2. _build_analyze_statement ===")
check("plain indexes only → no ANALYZE (it would be a placebo)",
      _build_analyze_statement([{"ddl": "CREATE INDEX i1 ON keyword(keyword);"},
                                {"ddl": "CREATE INDEX i2 ON title(production_year);"}]) == "")
out = _build_analyze_statement([{"ddl": "CREATE INDEX i1 ON keyword(keyword);"},
                                {"ddl": "CREATE INDEX i2 ON users (lower(email));"},
                                {"ddl": "CREATE INDEX i3 ON users ((id % 10));"}])
check("only the table with an expression index, once", out.endswith("\nANALYZE users;"))
check("Step 3 comment explains why", out.startswith("-- Step 3: Collect statistics for the expression indexes"))
check("no indexes → empty", _build_analyze_statement(None) == "" and _build_analyze_statement([]) == "")

print()
print("ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
