"""Tests for the sandbox benchmark's login (issue #1). Needs the job database and .env.

Sections 1-2 always run. Section 3 needs the sql_surgeon_sandbox role (scripts/setup_security_role.sql
with -v sb_password=...) and SURGEON_SANDBOX_DATABASE_URL; without it, it is skipped.

Run from backend/:  ../.venv/bin/python tests/test_sandbox_benchmark.py
"""
import json
import logging
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BACKEND, "..", ".env"))
import psycopg2  # noqa: E402
from sql_surgeon.db.client import DBClient, SandboxNotConfiguredError, UnsafeSQLError  # noqa: E402

logging.disable(logging.CRITICAL)  # the benchmark logs every expected failure below
ADMIN = os.environ["DATABASE_URL"]
SANDBOX = os.environ.get("SURGEON_SANDBOX_DATABASE_URL")
QUERY = "SELECT * FROM title WHERE production_year = 2000"
fails = 0


def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    fails += 0 if cond else 1


def client_with_sandbox(dsn):
    """A DBClient whose config sees the given sandbox DSN (config is read when the client is created)."""
    if dsn is None:
        os.environ.pop("SURGEON_SANDBOX_DATABASE_URL", None)
    else:
        os.environ["SURGEON_SANDBOX_DATABASE_URL"] = dsn
    return DBClient(ADMIN)


def raises(fn, exc):
    try:
        fn()
        return None
    except exc as e:
        return e
    except Exception as e:
        print(f"     unexpected: {type(e).__name__}: {e}")
        return None


def admin_one(sql):
    c = psycopg2.connect(ADMIN)
    c.autocommit = True
    try:
        cur = c.cursor()
        cur.execute(sql)
        return cur.fetchone()[0]
    finally:
        c.close()


print("=== 1. Not configured → refused, never falls back to DATABASE_URL ===")
e = raises(lambda: client_with_sandbox(None).benchmark_in_sandbox( QUERY, ""), SandboxNotConfiguredError)
check("SandboxNotConfiguredError", e is not None)
check("the message says how to fix it", e is not None and "SURGEON_SANDBOX_DATABASE_URL" in str(e)
      and "setup_security_role.sql" in str(e))

print("\n=== 2. Configured with a superuser login → refused before running anything ===")
e = raises(lambda: client_with_sandbox(ADMIN).benchmark_in_sandbox( QUERY, ""), UnsafeSQLError)
check(f"UnsafeSQLError: {str(e)[:80] if e else None}", e is not None and "superuser" in str(e))

print("\n=== 3. The sql_surgeon_sandbox login ===")
if not SANDBOX:
    print("  SKIP SURGEON_SANDBOX_DATABASE_URL is not set (run the setup script with -v sb_password=...)")
else:
    db = client_with_sandbox(SANDBOX)
    plan, _ = db.benchmark_in_sandbox( QUERY, "CREATE INDEX ON title(production_year);")
    check("a normal benchmark (index + EXPLAIN ANALYZE) still works", isinstance(plan, list) and "Plan" in plan[0])

    e = raises(lambda: db.benchmark_in_sandbox("WITH d AS (DELETE FROM public.kind_type RETURNING *) SELECT count(*) FROM d", ""),
        psycopg2.errors.InsufficientPrivilege)
    check("data-modifying CTE on a real table → permission denied", e is not None)

    victim = psycopg2.connect(ADMIN, application_name="sandbox_test_victim")
    e = raises(lambda: db.benchmark_in_sandbox("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'sandbox_test_victim'",
        ""), psycopg2.errors.InsufficientPrivilege)
    try:
        victim.cursor().execute("SELECT 1")
        alive = True
    except psycopg2.Error:
        alive = False
    victim.close()
    check("terminating another role's session → permission denied, the session survives", e is not None and alive)

    e = raises(lambda: db.benchmark_in_sandbox("SELECT set_config('role', 'postgres', true)", ""), psycopg2.errors.InsufficientPrivilege)
    check("switching to a privileged role from inside the query → permission denied", e is not None)
    plan, _ = db.benchmark_in_sandbox("SELECT * FROM title WHERE title LIKE '%Money%'",
                                   "CREATE EXTENSION IF NOT EXISTS pg_trgm; "
                                   "CREATE INDEX ON title USING gin (title gin_trgm_ops);")
    check("GH#2: a pg_trgm GIN index builds and the plan uses it", '"Bitmap Index Scan"' in json.dumps(plan))

    plan, _ = db.benchmark_in_sandbox("SELECT count(*) FROM title t JOIN kind_type k ON k.id = t.kind_id", "")
    check("every table the query reads is copied (from the plan, not a regex)", isinstance(plan, list) and "Plan" in plan[0])

    plan, _ = db.benchmark_in_sandbox("WITH x AS (SELECT * FROM kind_type) SELECT * FROM x", "")
    check("a CTE name isn't mistaken for a table", isinstance(plan, list) and "Plan" in plan[0])

    plan, skipped = db.benchmark_in_sandbox(QUERY, "CREATE INDEX ON company_name (name); "
                                                   "CREATE INDEX ON title (production_year);")
    check("an index on a table the query doesn't read is skipped, not run", skipped == ["CREATE INDEX ON company_name (name)"])
    check("the other index is still built and used", '"Index Name"' in json.dumps(plan))

    plan, skipped = db.benchmark_in_sandbox(QUERY, "ANALYZE title, company_name (name);")
    check("ANALYZE runs on the copies only and isn't reported as skipped", isinstance(plan, list) and skipped == [])

    admin_one("CREATE VIEW sandbox_test_view AS SELECT * FROM kind_type; "
              "GRANT SELECT ON sandbox_test_view TO sql_surgeon_sandbox; SELECT 1")
    try:
        e = raises(lambda: db.benchmark_in_sandbox("SELECT * FROM sandbox_test_view", ""), UnsafeSQLError)
    finally:
        admin_one("DROP VIEW sandbox_test_view; SELECT 1")
    check("a view (planned as its real tables) → refused before EXPLAIN ANALYZE",
          e is not None and "public.kind_type" in str(e))
    
    check("kind_type untouched", admin_one("SELECT count(*) FROM kind_type") == 7)
    check("no sandbox schema left behind",
          admin_one("SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'surgeon_tmp_%'") == 0)

print(f"\n{fails} failed")
sys.exit(1 if fails else 0)
