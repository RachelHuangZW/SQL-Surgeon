"""Tests for the SQL guards in sql_surgeon.db.client. Pure string tests — no database needed.

Run from backend/:  ../.venv/bin/python tests/test_sql_guards.py
"""
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
from sql_surgeon.db.client import (
    UnsafeSQLError,
    analyze_tables,
    benchmark_setup_statements,
    ensure_query_statement,
    ensure_single_statement,
    index_table,
    plan_relations,
    split_statements,
)

fails = 0

def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    fails += 0 if cond else 1


def rejected(fn, *args):
    try:
        fn(*args)
        return False
    except UnsafeSQLError:
        return True


print("=== 1. split_statements ===")
check("two statements", split_statements("SELECT 1; SELECT 2") == ["SELECT 1", " SELECT 2"])
check("trailing semicolons and comments are not statements", split_statements("SELECT 1; -- done\n;  /* x */ ") == ["SELECT 1"])
check("semicolon inside a string", split_statements("SELECT ';'; SELECT 2") == ["SELECT ';'", " SELECT 2"])
check("semicolon inside an E'' escape string", len(split_statements(r"SELECT E'a\';'; SELECT 2")) == 2)
check("semicolon inside a dollar quote", split_statements("SELECT $f$;$f$") == ["SELECT $f$;$f$"])
check("semicolon inside a nested block comment", split_statements("SELECT /* a /* ; */ ; */ 1") == ["SELECT /* a /* ; */ ; */ 1"])
check("semicolon inside a quoted identifier", split_statements('SELECT 1 AS "a;b"') == ['SELECT 1 AS "a;b"'])
check("only comments → empty", split_statements("-- nothing\n/* here */") == [])

print("\n=== 2. ensure_single_statement / ensure_query_statement ===")
check("single statement loses its trailing semicolon", ensure_single_statement("SELECT 1;  ") == "SELECT 1")
check("COMMIT escape is blocked", rejected(ensure_single_statement, "SELECT 1; COMMIT; DELETE FROM t"))
check("empty statement is blocked", rejected(ensure_single_statement, " ; -- x"))
for q in ["SELECT 1", "WITH a AS (SELECT 1) SELECT * FROM a", "VALUES (1)", "TABLE t", "(SELECT 1)", "-- c\nSELECT 1"]:
    check(f"allows {q!r}", not rejected(ensure_query_statement, q))
for q in ["DELETE FROM t", "UPDATE t SET a = 1", "INSERT INTO t VALUES (1)", "CREATE TABLE x (a int)",
          "DROP TABLE t", "TRUNCATE t", "CREATE TABLE x AS SELECT 1", "COPY t TO '/tmp/x'", "CALL p()"]:
    check(f"rejects {q!r}", rejected(ensure_query_statement, q))

print("\n=== 3. benchmark_setup_statements ===")
llm_script = """-- Step 1: Create indexes (run once)
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX idx_orders_amount ON orders(amount);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_t ON title USING gin (title gin_trgm_ops);
CREATE UNIQUE INDEX ON ONLY "Users" (email) WHERE active;
-- Step 2: Run the optimized query
SELECT * FROM orders WHERE amount > 50;

-- Step 3: Update planner statistics after index creation
ANALYZE orders, title (title);"""
got = benchmark_setup_statements(llm_script)
check("typical LLM script: 5 setup statements, SELECT skipped", len(got) == 5 and not any(s.upper().startswith("SELECT") for s in got))
check("CONCURRENTLY is removed", "CONCURRENTLY" not in got[2].upper() and got[2].startswith("CREATE INDEX IF NOT EXISTS idx_t ON title"))
check("empty script", benchmark_setup_statements("") == [])
for bad in [
    "DROP TABLE orders",
    "DELETE FROM public.orders",
    "ALTER TABLE public.orders DROP CONSTRAINT orders_pkey",
    "CREATE INDEX i ON public.orders (amount)",
    'CREATE INDEX i ON "public".orders (amount)',
    "ANALYZE public.orders",
    "ANALYZE",
    "CREATE EXTENSION dblink",
    "CREATE EXTENSION pg_trgm SCHEMA public",
    "CREATE TABLE orders AS SELECT 1",
    "COMMIT",
    "SET search_path TO public",
    "WITH d AS (DELETE FROM orders RETURNING *) SELECT 1",
]:
    # The data-modifying CTE starts with WITH, so it is "skipped" rather than rejected — it must never be returned
    if bad.startswith("WITH"):
        check(f"skipped, not run {bad!r}", benchmark_setup_statements(bad) == [])
    else:
        check(f"rejects {bad!r}", rejected(benchmark_setup_statements, bad))
check("a write mixed in with valid statements rejects the whole script",
      rejected(benchmark_setup_statements, "CREATE INDEX i ON orders (a);\nDELETE FROM public.orders WHERE id > 90;"))

print("\n=== 4. plan_relations ===")
join_plan = [{"Plan": {"Node Type": "Hash Join", "Plans": [
    {"Node Type": "Seq Scan", "Schema": "public", "Relation Name": "title", "Alias": "t1"},
    {"Node Type": "Hash", "Plans": [
        {"Node Type": "Nested Loop", "Plans": [
            {"Node Type": "Seq Scan", "Schema": "public", "Relation Name": "kind_type", "Alias": "k"},
            # Same table again under another alias (33b reads title as t1 and t2)
            {"Node Type": "Index Scan", "Schema": "public", "Relation Name": "title", "Alias": "t2"},
        ]},
    ]},
]}}]
check("every table at any depth, each once", plan_relations(join_plan) == {("public", "title"), ("public", "kind_type")})
check("join / hash nodes add nothing", plan_relations([{"Plan": {"Node Type": "Result"}}]) == set())
check("schema comes from the plan", plan_relations([{"Plan": {"Node Type": "Seq Scan", "Schema": "surgeon_tmp_ab12",
                                                              "Relation Name": "title"}}]) == {("surgeon_tmp_ab12", "title")})
check("empty set when there is no plan", plan_relations([]) == set())

print("\n=== 5. index_table / analyze_tables ===")
check("CREATE INDEX → its table", index_table("CREATE INDEX ON title (production_year)") == "title")
check("unquoted folds to lowercase", index_table("CREATE INDEX IF NOT EXISTS i ON ONLY Title (a)") == "title")
check("quoted keeps its case", index_table('CREATE INDEX i ON "Title" USING gin (x gin_trgm_ops)') == "Title")
check("not a CREATE INDEX → None", index_table("ANALYZE title") is None)
check("ANALYZE → every table, column lists ignored",
      analyze_tables("ANALYZE title, movie_info (info, note), Kind_Type") == ["title", "movie_info", "kind_type"])
check("ANALYZE VERBOSE", analyze_tables("ANALYZE VERBOSE title") == ["title"])
check("not an ANALYZE → []", analyze_tables("CREATE INDEX ON title (a)") == [])


print(f"\n{fails} failed")
sys.exit(1 if fails else 0)
