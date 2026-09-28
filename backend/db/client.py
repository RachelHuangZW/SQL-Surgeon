import re
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.errors
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import time

from db.config import load_security_config


class UnsafeSQLError(ValueError):
    """User SQL was rejected before or during execution for safety reasons."""


class QueryTimeoutError(TimeoutError):
    """User SQL exceeded the configured statement_timeout."""


# One pool per DSN, shared across DBClient instances (nodes create a new DBClient per call).
_pools = {}
_pools_lock = threading.Lock()
_warned_no_readonly_dsn = False


def _get_pool(dsn: str, minconn: int, maxconn: int) -> pool.ThreadedConnectionPool:
    with _pools_lock:
        p = _pools.get(dsn)
        if p is None:
            p = pool.ThreadedConnectionPool(minconn, maxconn, dsn, application_name="sql_surgeon_readonly")
            _pools[dsn] = p
        return p


_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def ensure_single_statement(sql: str) -> str:
    """Return sql with trailing semicolons stripped; raise UnsafeSQLError if it holds more than one statement.

    psycopg2 sends queries over the simple protocol, which accepts multiple statements.
    Without this check `SELECT 1; COMMIT; DROP TABLE t` would end our READ ONLY transaction
    and run the DROP outside of it. Skips semicolons inside quotes, comments and dollar-quoted bodies.
    """
    i, n = 0, len(sql)
    end = None  # index of the first top-level ';'
    while i < n:
        c = sql[i]
        if end is not None and not (c.isspace() or c == ";" or sql.startswith(("--", "/*"), i)):
            raise UnsafeSQLError("Only a single SQL statement is allowed")

        if c == "'":
            # E'...' strings honor backslash escapes; '' is an escaped quote in both forms
            backslash_escapes = i > 0 and sql[i - 1] in "eE" and (i < 2 or not _is_ident_char(sql[i - 2]))
            i += 1
            while i < n:
                if backslash_escapes and sql[i] == "\\":
                    i += 2
                elif sql[i] == "'":
                    if sql.startswith("''", i):
                        i += 2
                    else:
                        break
                else:
                    i += 1
            i += 1
        elif c == '"':
            # "" inside a quoted identifier is just two adjacent quoted segments
            j = sql.find('"', i + 1)
            i = n if j == -1 else j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
        elif sql.startswith("/*", i):
            # block comments nest in PostgreSQL
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        elif c == "$" and not (i > 0 and _is_ident_char(sql[i - 1])) and _DOLLAR_TAG.match(sql, i):
            tag = _DOLLAR_TAG.match(sql, i).group(0)
            j = sql.find(tag, i + len(tag))
            i = n if j == -1 else j + len(tag)
        elif c == ";":
            if end is None:
                end = i
            i += 1
        else:
            i += 1

    statement = sql if end is None else sql[:end]
    if not statement.strip():
        raise UnsafeSQLError("Empty SQL statement")
    return statement


# EXPLAIN also accepts DML, EXECUTE, DECLARE, CREATE TABLE AS and CREATE MATERIALIZED VIEW.
# PostgreSQL does NOT apply the READ ONLY check to `EXPLAIN ANALYZE CREATE TABLE AS`
# (verified on 17), so only query forms are let through.
_ALLOWED_LEADING_KEYWORDS = {"SELECT", "WITH", "VALUES", "TABLE"}
_LEADING_NOISE = re.compile(r"(?:\s+|--[^\n]*(?:\n|$)|\(|/\*.*?\*/)*", re.DOTALL)


def ensure_query_statement(statement: str) -> None:
    """Raise UnsafeSQLError unless the statement is a SELECT / WITH / VALUES / TABLE query."""
    # Non-greedy /* */ skip doesn't handle nested comments; a mismatch only causes a false rejection.
    m = re.match(r"([A-Za-z]+)", statement[_LEADING_NOISE.match(statement).end():])
    keyword = m.group(1).upper() if m else ""
    if keyword not in _ALLOWED_LEADING_KEYWORDS:
        raise UnsafeSQLError(
            f"Only read-only queries (SELECT / WITH / VALUES / TABLE) can be analyzed, got {keyword or 'unknown'!r}"
        )


class DBClient:
    def __init__(self, dsn):
        self.dsn = dsn
        self.config = load_security_config()
        # DSN for read-only
        self.readonly_dsn = self.config.readonly_dsn or dsn

    def _warn_if_shared_dsn(self):
        global _warned_no_readonly_dsn
        if self.config.readonly_dsn is None and not _warned_no_readonly_dsn:
            _warned_no_readonly_dsn = True
            print("WARNING: SURGEON_READONLY_DATABASE_URL not set; running user SQL with DATABASE_URL "
                  "privileges. See scripts/setup_security_role.sql.")

    @contextmanager
    def _readonly_transaction(self):
        """Yield a pooled connection inside a READ ONLY transaction with statement/lock timeouts.

        The transaction is always rolled back and the session reset before the connection goes
        back to the pool; a connection that can't be cleaned up is closed instead of reused.
        """
        self._warn_if_shared_dsn()
        conn_pool = _get_pool(self.readonly_dsn, self.config.pool_min, self.config.pool_max)
        try:
            conn = conn_pool.getconn()
        except pool.PoolError as e:
            raise RuntimeError(f"Database connection pool exhausted (max {self.config.pool_max})") from e

        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                # psycopg2 opens the transaction implicitly on the first execute,
                # so these apply to the same transaction the user SQL runs in.
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute("SET LOCAL statement_timeout = %s", (self.config.statement_timeout_ms,)) # Set timeout for EXPLAIN ANALYZE
                cur.execute("SET LOCAL lock_timeout = %s", (self.config.lock_timeout_ms,))
            yield conn
        finally:
            # EXPLAIN ANALYZE physically executes the query, so rollback to prevent
            # accidental writes if the input SQL contains CTEs or side-effecting functions.
            reusable = not conn.closed
            if reusable:
                try:
                    conn.rollback()
                    # DISCARD ALL drops session state the query may have left behind
                    # (e.g. session-level advisory locks survive ROLLBACK). Not allowed inside a transaction.
                    conn.autocommit = True
                    with conn.cursor() as cur:
                        cur.execute("DISCARD ALL")
                    conn.autocommit = False
                except psycopg2.Error:
                    reusable = False
            conn_pool.putconn(conn, close=not reusable)

    def execute_explain(self, sql: str):
        # Defense in depth for user SQL: single statement -> query-only allowlist ->
        # READ ONLY transaction -> statement_timeout -> least-privilege role (SURGEON_READONLY_DATABASE_URL).
        statement = ensure_single_statement(sql)
        ensure_query_statement(statement)
        explain_query = f"EXPLAIN (ANALYZE, COSTS, VERBOSE, BUFFERS, FORMAT JSON) {statement}"

        try:
            with self._readonly_transaction() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute(explain_query)
                    result = cursor.fetchone()
                    return result["QUERY PLAN"]

        except psycopg2.errors.QueryCanceled as e:
            print(f"Explain Plan timed out: {e}")
            raise QueryTimeoutError(
                f"Query exceeded statement_timeout of {self.config.statement_timeout_ms} ms "
                f"(adjust SURGEON_STATEMENT_TIMEOUT_MS)"
            ) from e

        except psycopg2.errors.ReadOnlySqlTransaction as e:
            print(f"Blocked write in Explain Plan: {e}")
            raise UnsafeSQLError(f"Write operations are not allowed: {e}") from e

        except Exception as e:
            print(f"Error executing Explain Plan: {e}")
            raise e

    
    def benchmark_in_sandbox(self, table_names: list, original_sql: str, suggested_ddl: str):
        conn = psycopg2.connect(self.dsn)
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '300s'") # Set timeout for DDL + EXPLAIN ANALYZE
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        schema_name = f"surgeon_tmp_{int(time.time())}"

        try:
            cursor.execute(f"CREATE SCHEMA {schema_name};")
            for table in table_names:
                cursor.execute(f"CREATE TABLE {schema_name}.{table} (LIKE public.{table} INCLUDING DEFAULTS INCLUDING INDEXES);")
                cursor.execute(f"INSERT INTO {schema_name}.{table} SELECT * FROM public.{table} LIMIT 100000;")
            
            cursor.execute(f"SET search_path TO {schema_name};")

            if suggested_ddl:
                cursor.execute(suggested_ddl)
            
            cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {original_sql}")
            result = cursor.fetchone()
            new_plan = result["QUERY PLAN"]
            
            return new_plan

        except Exception as e:
            conn.rollback()
            print(f"Error creating benchmark schema: {e}")
            raise e

        finally:
            cursor.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE;")
            conn.commit()
            cursor.close()
            conn.close()   




