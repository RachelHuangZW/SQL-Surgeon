"""Tests for DBClient.check_index_ddls against the job database (needs job-pg running and hypopg installed).

Run from backend/:  ../.venv/bin/python tests/test_index_check.py
"""
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BACKEND, "..", ".env"))
# One pooled connection, so the leftover check below sees the same session check_index_ddls used
os.environ["SURGEON_DB_POOL_MAX"] = "1"
from sql_surgeon.db.client import DBClient, UNVERIFIED  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    fails += 0 if cond else 1


db = DBClient(os.environ["DATABASE_URL"])


def one(ddl):
    return db.check_index_ddls([ddl])[ddl]


def leftover_hypothetical_indexes():
    with db._readonly_transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM hypopg()")
            return cur.fetchone()[0]


print("=== 1. Valid indexes → None ===")
check("plain B-tree", one("CREATE INDEX ON title(production_year)") is None)
check("named, multi-column", one("CREATE INDEX idx_t ON title(kind_id, production_year)") is None)
check("operator class text_pattern_ops", one("CREATE INDEX ON name(name text_pattern_ops)") is None)
check("expression index (info::numeric)", one("CREATE INDEX ON movie_info_idx ((info::numeric))") is None)
check("trailing semicolon", one("CREATE INDEX ON title(production_year);") is None)

print("\n=== 2. Hallucinated tables / columns → PostgreSQL's error ===")
r = one("CREATE INDEX ON title(tconst)")
check(f"made-up column tconst → {r!r}", r not in (None, UNVERIFIED) and "tconst" in r)
r = one("CREATE INDEX idx_t ON title(primary_title, start_year)")
check(f"made-up column primary_title → {r!r}", r not in (None, UNVERIFIED) and "primary_title" in r)
r = one("CREATE INDEX ON titles(id)")
check(f"made-up table titles → {r!r}", r not in (None, UNVERIFIED) and "titles" in r)

print("\n=== 3. Index types hypopg can't simulate → UNVERIFIED (kept, not claimed valid) ===")
check("GIN with gin_trgm_ops", one("CREATE INDEX ON title USING gin (title gin_trgm_ops)") == UNVERIFIED)

print("\n=== 4. Anything that isn't one CREATE INDEX is rejected without running it ===")
r = one("DROP TABLE kind_type")
check(f"DROP TABLE → {r!r}", r not in (None, UNVERIFIED))
r = one("CREATE INDEX ON title(production_year); DROP TABLE kind_type")
check(f"CREATE INDEX with a second statement smuggled in → {r!r}", r not in (None, UNVERIFIED))
check("kind_type still exists", db.run_query("SELECT count(*) AS n FROM kind_type")[0]["n"] == 7)

print("\n=== 5. Batch: one bad DDL doesn't affect the others ===")
batch = [
    "CREATE INDEX ON title(production_year)",
    "CREATE INDEX ON title(tconst)",
    "CREATE INDEX ON movie_companies(company_id)",
]
r = db.check_index_ddls(batch)
check("valid / invalid / valid → None / error / None",
      r[batch[0]] is None and r[batch[1]] not in (None, UNVERIFIED) and r[batch[2]] is None)
check("empty list → empty dict", db.check_index_ddls([]) == {})

print("\n=== 6. No hypothetical index survives on the pooled connection ===")
check("hypopg() is empty after the calls above (incl. ones that hit errors)", leftover_hypothetical_indexes() == 0)

print("\n=== 7. review_advice drops what PostgreSQL rejects (LLM stubbed) ===")
import json  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.runnables import RunnableLambda  # noqa: E402
import sql_surgeon.agent.nodes as nodes  # noqa: E402

GOOD = "CREATE INDEX idx_t_year ON title(production_year)"
BAD = "CREATE INDEX idx_t_tconst ON title(tconst)"
GIN = "CREATE INDEX idx_t_trgm ON title USING gin (title gin_trgm_ops)"
indexes = [{"ddl": d + ";", "reason": "r"} for d in (GOOD, BAD, GIN)]
script = (f"-- Step 1: Create indexes (run once)\n{GOOD};\n{BAD};\n{GIN};\n\n"
          "-- Step 2: Run the optimized query\nSELECT * FROM title WHERE production_year = 2000;")
# The reviewer passes everything: the hard check must not depend on the LLM catching it
review = {"verdict": "pass", "feedback": "", "filtered_indexes": indexes, "filtered_optimized_sql": script}
nodes.llm = RunnableLambda(lambda _: AIMessage(content=json.dumps(review)))

# retry_count 1 = no retry left (see section 8), so this is the drop-and-warn path
out = nodes.review_advice({"ddl": "", "filtered_indexes": indexes, "advice": [], "optimized_sql": script,
                           "issues": [], "small_tables": {}, "retry_count": 1})
kept = " ".join(i["ddl"] for i in out["filtered_indexes"])
check("made-up column dropped from the index list", "tconst" not in kept)
check("made-up column dropped from the script", "tconst" not in out["optimized_sql"])
check("valid B-tree and unverifiable GIN kept in both", all(n in kept and n in out["optimized_sql"]
                                                          for n in ("idx_t_year", "idx_t_trgm")))
check("user is told what was dropped and why",
      any("tconst" in w and "does not exist" in w for w in out["rewrite_warnings"]))
check("hypopg() still empty after the node ran", leftover_hypothetical_indexes() == 0)

print("\n=== 8. Rejected indexes send the advice back for a retry while one is left ===")
from sql_surgeon.agent.graph import route_after_review  # noqa: E402


def review_with(verdict, feedback, retry_count, idx=indexes, sql=script):
    nodes.llm = RunnableLambda(lambda _: AIMessage(content=json.dumps(
        {"verdict": verdict, "feedback": feedback, "filtered_indexes": idx, "filtered_optimized_sql": sql})))
    state = {"ddl": "", "filtered_indexes": idx, "advice": [], "optimized_sql": sql,
             "issues": [], "small_tables": {}, "retry_count": retry_count}
    return {**state, **nodes.review_advice(state)}


s = review_with("pass", "", 0)
check("reviewer passed, but tconst is rejected → verdict retry, retry_count 1",
      s["verdict"] == "retry" and s["retry_count"] == 1)
check("the graph really routes back to generate_advice", route_after_review(s) == "retry")
check("feedback names the rejected index and the reason", "tconst" in s["feedback"] and "does not exist" in s["feedback"])
check("feedback lists title's real columns (generate_advice gets no DDL)", "production_year" in s["feedback"])
check("no 'Dropped' warning yet: the user never sees this attempt",
      not any("Dropped a recommended index" in w for w in s.get("rewrite_warnings") or []))

s = review_with("retry", "Index order is wrong.", 0)
check("reviewer already asked for a retry → counted once, not twice", s["retry_count"] == 1)
check("both the reviewer's and the schema feedback reach generate_advice",
      "Index order is wrong." in s["feedback"] and "tconst" in s["feedback"])

s = review_with("pass", "", 1)
check("no retry left → stays pass and ends", s["verdict"] == "pass" and route_after_review(s) == "end")

ok = [i for i in indexes if "tconst" not in i["ddl"]]
ok_script = "\n".join(line for line in script.splitlines() if "tconst" not in line)
s = review_with("pass", "", 0, idx=ok, sql=ok_script)
check("nothing rejected → verdict, feedback and retry_count untouched",
      s["verdict"] == "pass" and s["feedback"] == "" and s["retry_count"] == 0)

print(f"\n{fails} failed")
sys.exit(1 if fails else 0)
