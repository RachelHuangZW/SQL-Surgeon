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
check("两条语句", split_statements("SELECT 1; SELECT 2") == ["SELECT 1", " SELECT 2"])
check("末尾分号和注释不算语句", split_statements("SELECT 1; -- done\n;  /* x */ ") == ["SELECT 1"])
check("字符串里的分号", split_statements("SELECT ';'; SELECT 2") == ["SELECT ';'", " SELECT 2"])
check("E'' 转义里的分号", len(split_statements(r"SELECT E'a\';'; SELECT 2")) == 2)
check("dollar quote 里的分号", split_statements("SELECT $f$;$f$") == ["SELECT $f$;$f$"])
check("嵌套块注释里的分号", split_statements("SELECT /* a /* ; */ ; */ 1") == ["SELECT /* a /* ; */ ; */ 1"])
check("引号标识符里的分号", split_statements('SELECT 1 AS "a;b"') == ['SELECT 1 AS "a;b"'])
check("只有注释 → 空", split_statements("-- nothing\n/* here */") == [])

print("\n=== 2. ensure_single_statement / ensure_query_statement ===")
check("单条语句去掉末尾分号", ensure_single_statement("SELECT 1;  ") == "SELECT 1")
check("COMMIT 逃逸被挡", rejected(ensure_single_statement, "SELECT 1; COMMIT; DELETE FROM t"))
check("空语句被挡", rejected(ensure_single_statement, " ; -- x"))
for q in ["SELECT 1", "WITH a AS (SELECT 1) SELECT * FROM a", "VALUES (1)", "TABLE t", "(SELECT 1)", "-- c\nSELECT 1"]:
    check(f"放行 {q!r}", not rejected(ensure_query_statement, q))
for q in ["DELETE FROM t", "UPDATE t SET a = 1", "INSERT INTO t VALUES (1)", "CREATE TABLE x (a int)",
          "DROP TABLE t", "TRUNCATE t", "CREATE TABLE x AS SELECT 1", "COPY t TO '/tmp/x'", "CALL p()"]:
    check(f"拒绝 {q!r}", rejected(ensure_query_statement, q))

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
check("典型 LLM 脚本：5 条 setup，跳过 SELECT", len(got) == 5 and not any(s.upper().startswith("SELECT") for s in got))
check("CONCURRENTLY 被去掉", "CONCURRENTLY" not in got[2].upper() and got[2].startswith("CREATE INDEX IF NOT EXISTS idx_t ON title"))
check("空脚本", benchmark_setup_statements("") == [])
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
        check(f"跳过不执行 {bad!r}", benchmark_setup_statements(bad) == [])
    else:
        check(f"拒绝 {bad!r}", rejected(benchmark_setup_statements, bad))
check("混在合法语句里的写操作也会让整个脚本被拒",
      rejected(benchmark_setup_statements, "CREATE INDEX i ON orders (a);\nDELETE FROM public.orders WHERE id > 90;"))

print(f"\n{fails} 个失败")
sys.exit(1 if fails else 0)
