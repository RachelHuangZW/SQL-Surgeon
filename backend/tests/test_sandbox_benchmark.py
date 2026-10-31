"""Tests for the sandbox benchmark's login (issue #1). Needs the job database and .env.

Sections 1-2 always run. Section 3 needs the sql_surgeon_sandbox role (scripts/setup_security_role.sql
with -v sb_password=...) and SURGEON_SANDBOX_DATABASE_URL; without it, it is skipped.

Run from backend/:  ../.venv/bin/python tests/test_sandbox_benchmark.py
"""
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
e = raises(lambda: client_with_sandbox(None).benchmark_in_sandbox(["title"], QUERY, ""), SandboxNotConfiguredError)
check("SandboxNotConfiguredError", e is not None)
check("the message says how to fix it", e is not None and "SURGEON_SANDBOX_DATABASE_URL" in str(e)
      and "setup_security_role.sql" in str(e))

print("\n=== 2. Configured with a superuser login → refused before running anything ===")
e = raises(lambda: client_with_sandbox(ADMIN).benchmark_in_sandbox(["title"], QUERY, ""), UnsafeSQLError)
check(f"UnsafeSQLError: {str(e)[:80] if e else None}", e is not None and "superuser" in str(e))

print("\n=== 3. The sql_surgeon_sandbox login ===")
if not SANDBOX:
    print("  SKIP SURGEON_SANDBOX_DATABASE_URL is not set (run the setup script with -v sb_password=...)")
else:
    db = client_with_sandbox(SANDBOX)
    plan = db.benchmark_in_sandbox(["title"], QUERY, "CREATE INDEX ON title(production_year);")
    check("a normal benchmark (index + EXPLAIN ANALYZE) still works", isinstance(plan, list) and "Plan" in plan[0])

    e = raises(lambda: db.benchmark_in_sandbox(
        ["title"], "WITH d AS (DELETE FROM public.kind_type RETURNING *) SELECT count(*) FROM d", ""),
        psycopg2.errors.InsufficientPrivilege)
    check("data-modifying CTE on a real table → permission denied", e is not None)

    victim = psycopg2.connect(ADMIN, application_name="sandbox_test_victim")
    e = raises(lambda: db.benchmark_in_sandbox(
        ["title"], "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'sandbox_test_victim'",
        ""), psycopg2.errors.InsufficientPrivilege)
    try:
        victim.cursor().execute("SELECT 1")
        alive = True
    except psycopg2.Error:
        alive = False
    victim.close()
    check("terminating another role's session → permission denied, the session survives", e is not None and alive)

    e = raises(lambda: db.benchmark_in_sandbox(
        ["title"], "SELECT set_config('role', 'postgres', true)", ""), psycopg2.errors.InsufficientPrivilege)
    check("switching to a privileged role from inside the query → permission denied", e is not None)

    check("kind_type untouched", admin_one("SELECT count(*) FROM kind_type") == 7)
    check("no sandbox schema left behind",
          admin_one("SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'surgeon_tmp_%'") == 0)

print(f"\n{fails} failed")
sys.exit(1 if fails else 0)
