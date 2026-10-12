"""Tests for the SQL guards in sql_surgeon.db.client. Pure string tests — no database needed.

Run from backend/:  ../.venv/bin/python tests/test_sql_guards.py
"""
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
from sql_surgeon.db.client import (  # noqa: E402
    UnsafeSQLError,
    benchmark_setup_statements,
    ensure_query_statement,
    ensure_single_statement,
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

print(f"\n{fails} failed")
sys.exit(1 if fails else 0)
