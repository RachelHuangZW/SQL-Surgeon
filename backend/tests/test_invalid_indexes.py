"""Tests for dropping recommended indexes PostgreSQL rejects (issue #9). Pure tests — no database or LLM call.

Run from backend/:  ../.venv/bin/python tests/test_invalid_indexes.py
"""
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
from sql_surgeon.agent.nodes import (  # noqa: E402
    ddl_key, drop_invalid_indexes, drop_invalid_statements, invalid_index_feedback, invalid_index_warnings,
    recommended_index_ddls)

fails = 0


def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    fails += 0 if cond else 1


GOOD = "CREATE INDEX idx_t_year ON title(production_year)"
BAD = "CREATE INDEX idx_t_tconst ON title(tconst)"
ERROR = 'hypopg: column "tconst" does not exist'
invalid = {ddl_key(BAD): ERROR}

script = f"""-- Step 1: Create indexes (run once)
{GOOD};
{BAD};

-- Step 2: Run the optimized query
SELECT * FROM title WHERE production_year = 2000;"""

print("=== 1. ddl_key ===")
check("ignores whitespace, case and the trailing semicolon",
      ddl_key("create  index idx_t_tconst\n ON title(tconst);") == ddl_key(BAD))
check("different statements get different keys", ddl_key(GOOD) != ddl_key(BAD))

print("=== 2. recommended_index_ddls ===")
got = recommended_index_ddls([{"ddl": GOOD + ";", "reason": "r"}, {"ddl": BAD, "reason": "r"}], script)
check(f"each CREATE INDEX once, list first then script → {len(got)}", [ddl_key(d) for d in got] == [ddl_key(GOOD), ddl_key(BAD)])
check("the query in Step 2 is not collected", not any("SELECT" in d.upper() for d in got))
check("nothing to check → empty list", recommended_index_ddls(None, None) == [] and recommended_index_ddls([], "") == [])

print("=== 3. drop_invalid_indexes ===")
kept, dropped = drop_invalid_indexes([{"ddl": GOOD, "reason": "r"}, {"ddl": BAD + ";", "reason": "r"}], invalid)
check("valid index kept", [i["ddl"] for i in kept] == [GOOD])
check("invalid index dropped with its error", dropped == [(BAD + ";", ERROR)])
check("no invalid indexes → everything kept", drop_invalid_indexes([{"ddl": GOOD}], {})[0] == [{"ddl": GOOD}])
check("None list → ([], [])", drop_invalid_indexes(None, invalid) == ([], []))

print("=== 4. drop_invalid_statements ===")
out, dropped = drop_invalid_statements(script, invalid)
check("invalid statement removed from the script", "tconst" not in out)
check("valid index, Step comments and the query kept",
      GOOD in out and "-- Step 1" in out and "-- Step 2" in out and "production_year = 2000" in out)
check("dropped reports the statement and its error", dropped == [(BAD + ";", ERROR)])
out, _ = drop_invalid_statements(f"{GOOD};\n{BAD}", invalid)
check("last statement without a semicolon is removed too", "tconst" not in out and GOOD in out)
check("None script → ('', [])", drop_invalid_statements(None, invalid) == ("", []))

print("=== 5. invalid_index_warnings ===")
w = invalid_index_warnings([(BAD, ERROR), (BAD + ";", ERROR)])
check("one warning per distinct index", len(w) == 1)
check("names the index and PostgreSQL's reason, without the hypopg: prefix",
      "tconst" in w[0] and 'column "tconst" does not exist' in w[0] and "hypopg:" not in w[0])

print("=== 6. invalid_index_feedback (retry feedback for generate_advice) ===")
metadata = {"title": {"columns": [("id", "integer"), ("title", "text"), ("production_year", "integer")], "indexes": []}}
fb = invalid_index_feedback([(BAD, ERROR), (BAD + ";", ERROR),
                             ("CREATE INDEX ON titles(id)", 'relation "titles" does not exist')], metadata)
items = [line for line in fb.splitlines() if line.startswith("- ")]
check("names each rejected index once (the duplicate BAD is listed once)", len(items) == 2)
check("with PostgreSQL's reason, without the hypopg: prefix",
      'column "tconst" does not exist' in fb and "hypopg:" not in fb)
check("lists the table's real columns, since generate_advice never sees the DDL",
      "Columns of title: id, title, production_year" in fb)
check("says so when the table itself doesn't exist", "Table titles does not exist" in fb)

print()
print("ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
