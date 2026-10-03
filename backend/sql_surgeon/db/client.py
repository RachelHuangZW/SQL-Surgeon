import re
import threading
import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.errors
from psycopg2 import pool
from psycopg2 import sql as pgsql
from psycopg2.extras import RealDictCursor
import uuid

from .config import load_security_config


class UnsafeSQLError(ValueError):
    """User SQL was rejected before or during execution for safety reasons."""


class QueryTimeoutError(TimeoutError):
    """User SQL exceeded the configured statement_timeout."""


# One pool per DSN, shared across DBClient instances (nodes create a new DBClient per call).
_pools = {}
_pools_lock = threading.Lock()
_warned_no_readonly_dsn = False

logger = logging.getLogger(__name__)


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


def split_statements(sql: str) -> list:
    """Split sql on top-level semicolons, skipping those inside quotes, comments and dollar-quoted bodies.

    Returns the statements without their terminating ';'. Pieces holding only whitespace and
    comments (e.g. after the last ';') are dropped.
    """
    statements = []
    i, n = 0, len(sql)
    start, has_code = 0, False
    while i < n:
        c = sql[i]
        if c == "'":
            # E'...' strings honor backslash escapes; '' is an escaped quote in both forms
            backslash_escapes = i > 0 and sql[i - 1] in "eE" and (i < 2 or not _is_ident_char(sql[i - 2]))
            has_code = True
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
            has_code = True
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
            has_code = True
            j = sql.find(tag, i + len(tag))
            i = n if j == -1 else j + len(tag)
        elif c == ";":
            if has_code:
                statements.append(sql[start:i])
            start, has_code = i + 1, False
            i += 1
        else:
            has_code = has_code or not c.isspace()
            i += 1

    if has_code:
        statements.append(sql[start:])
    return statements


def ensure_single_statement(sql: str) -> str:
    """Return the one statement in sql (trailing semicolons stripped); raise UnsafeSQLError otherwise.

    psycopg2 sends queries over the simple protocol, which accepts multiple statements.
    Without this check `SELECT 1; COMMIT; DROP TABLE t` would end our READ ONLY transaction
    and run the DROP outside of it.
    """
    statements = split_statements(sql)
    if not statements:
        raise UnsafeSQLError("Empty SQL statement")
    if len(statements) > 1:
        raise UnsafeSQLError("Only a single SQL statement is allowed")
    return statements[0]


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
            f"Only read-only queries (SELECT / WITH / VALUES / TABLE) are allowed, got {keyword or 'unknown'!r}"
        )


# Unqualified on purpose: with search_path set to the sandbox schema these can only reach the copies.
_IDENT = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"[^".]+")'
_LEADING_COMMENTS = re.compile(r"(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.DOTALL)
_CREATE_INDEX = re.compile(
    rf"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?P<concurrently>CONCURRENTLY\s+)?"
    rf"(?:(?:IF\s+NOT\s+EXISTS\s+)?{_IDENT}\s+)?ON\s+(?:ONLY\s+)?{_IDENT}\s*(?:USING\s+\w+\s*)?\(",
    re.IGNORECASE,
)
_SANDBOX_EXTENSIONS = r"(?:pg_trgm|btree_gin|btree_gist)"
_CREATE_EXTENSION = re.compile(
    rf'CREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:{_SANDBOX_EXTENSIONS}|"{_SANDBOX_EXTENSIONS}")\s*',
    re.IGNORECASE,
)
_COLUMNS = r"(?:\s*\(\s*[^()]*\))?"
_ANALYZE = re.compile(rf"ANALYZE\s+(?:VERBOSE\s+)?{_IDENT}{_COLUMNS}(?:\s*,\s*{_IDENT}{_COLUMNS})*\s*", re.IGNORECASE)


def benchmark_setup_statements(ddl: str) -> list:
    """Return the statements of an LLM-suggested script that may run in the benchmark sandbox.

    Allowed: CREATE INDEX, CREATE EXTENSION (pg_trgm / btree_gin / btree_gist) and ANALYZE, on
    unqualified table names only. Query statements (the script's "Step 2") are skipped because the
    benchmark runs the query itself. Anything else raises UnsafeSQLError.
    CONCURRENTLY is dropped: it can't run inside the benchmark's transaction.
    """
    allowed = []
    for statement in split_statements(ddl):
        body = statement[_LEADING_COMMENTS.match(statement).end():].rstrip()
        m = _CREATE_INDEX.match(body)
        if m:
            if m.group("concurrently"):
                body = body[:m.start("concurrently")] + body[m.end("concurrently"):]
            allowed.append(body)
        elif _CREATE_EXTENSION.fullmatch(body) or _ANALYZE.fullmatch(body):
            allowed.append(body)
        else:
            try:
                ensure_query_statement(body)
            except UnsafeSQLError:
                raise UnsafeSQLError(f"Statement not allowed in benchmark sandbox: {body[:200]!r}") from None
    return allowed


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
            logger.warning("WARNING: SURGEON_READONLY_DATABASE_URL not set; running user SQL with DATABASE_URL "
                           "privileges. See scripts/setup_security_role.sql.")
    
    @contextmanager
    def _get_connection(self):
        """Borrow a pooled connection on the read-only DSN and always hand it back clean.

        The connection is rolled back and its session reset before returning to the pool;
        a connection that can't be cleaned up is closed instead of reused.
        """
        self._warn_if_shared_dsn()
        conn_pool = _get_pool(self.readonly_dsn, self.config.pool_min, self.config.pool_max)

        try:
            conn = conn_pool.getconn()
        except pool.PoolError as e:
            raise RuntimeError(f"Database connection pool exhausted (max {self.config.pool_max})") from e

        try:
            yield conn
        finally:
            reusable = not conn.closed
            if reusable:
                try:
                    conn.rollback()
                    conn.autocommit = True
                    with conn.cursor() as cur:
                        cur.execute("DISCARD ALL")
                    conn.autocommit = False
                except psycopg2.Error:
                    reusable = False
            conn_pool.putconn(conn, close=not reusable)

    @contextmanager
    def _readonly_transaction(self):
        """Yield a pooled connection inside a READ ONLY transaction with statement/lock timeouts.

        Cleanup (rollback, session reset, return to pool) is handled by _get_connection.
        """
        with self._get_connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute("SET LOCAL statement_timeout = %s", (self.config.statement_timeout_ms,)) # Set timeout for EXPLAIN ANALYZE
                cur.execute("SET LOCAL lock_timeout = %s", (self.config.lock_timeout_ms,))

            yield conn
    
    def get_primary_keys(self, tables: list) -> list:
        """Return [(table_name, column_name), ...] for primary-key columns of the given public tables."""
        with self._readonly_transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.relname AS table_name, a.attname AS column_name
                    FROM pg_constraint con
                    JOIN pg_class c     ON c.oid = con.conrelid          -- 表 OID → 表名
                    JOIN pg_namespace n ON n.oid = c.relnamespace        -- → schema 名
                    CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)  -- 数组拆成多行
                    JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum -- 列号 → 列名
                    WHERE con.contype = 'p'
                      AND n.nspname = 'public'
                      AND c.relname = ANY(%s)
                    ORDER BY k.ord;
                    """,
                    (tables,),
                )
                return cur.fetchall()
        
    def get_table_metadata(self, tables: list) -> dict:
        """Return {table_name: {"columns": [(name, type), ...], "indexes": [(indexname, indexdef), ...]}}.

        Only tables that actually exist in schema public appear as keys.
        """
        metadata = {}
        with self._readonly_transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name, column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = ANY(%s)
                    ORDER BY table_name, ordinal_position
                    """,
                    (tables,),
                )
                for table, col, dtype in cur.fetchall():
                    # First row of a table creates its entry; later rows append to it
                    metadata.setdefault(table, {"columns": [], "indexes": []})["columns"].append((col, dtype))

                cur.execute(
                    """
                    SELECT tablename, indexname, indexdef
                    FROM pg_indexes
                    WHERE schemaname = 'public' AND tablename = ANY(%s)
                    ORDER BY tablename, indexname
                    """,
                    (tables,),
                )
                for table, idx_name, idx_def in cur.fetchall():
                    # A table with indexes always has columns, so its entry already exists
                    metadata[table]["indexes"].append((idx_name, idx_def))

        return metadata
        
        
    def _run_user_sql(self, query: str, fetch):
        """Run validated user SQL in a READ ONLY transaction and return fetch(cursor).

        Defense in depth: callers have already applied the single-statement check and the
        query-only allowlist; this adds the READ ONLY transaction, statement_timeout and the
        least-privilege role (SURGEON_READONLY_DATABASE_URL). Nothing is ever committed.
        """
        try:
            with self._readonly_transaction() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute(query)
                    return fetch(cursor)

        except psycopg2.errors.QueryCanceled as e:
            logger.warning(f"User SQL timed out: {e}")
            raise QueryTimeoutError(
                f"Query exceeded statement_timeout of {self.config.statement_timeout_ms} ms "
                f"(adjust SURGEON_STATEMENT_TIMEOUT_MS)"
            ) from e

        except psycopg2.errors.ReadOnlySqlTransaction as e:
            logger.warning(f"Blocked write in user SQL: {e}")
            raise UnsafeSQLError(f"Write operations are not allowed: {e}") from e

        except Exception as e:
            logger.error(f"Error executing user SQL: {e}")
            raise e

    def execute_explain(self, sql: str):
        statement = ensure_single_statement(sql)
        ensure_query_statement(statement)
        return self._run_user_sql(
            f"EXPLAIN (ANALYZE, COSTS, VERBOSE, BUFFERS, FORMAT JSON) {statement}",
            lambda cursor: cursor.fetchone()["QUERY PLAN"],
        )

    def explain_text(self, sql: str, analyze: bool = False) -> str:
        """Return the text EXPLAIN plan of a read-only query (EXPLAIN ANALYZE when analyze=True)."""
        statement = ensure_single_statement(sql)
        ensure_query_statement(statement)
        prefix = "EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)" if analyze else "EXPLAIN (FORMAT TEXT)"
        return self._run_user_sql(
            f"{prefix} {statement}",
            lambda cursor: "\n".join(row["QUERY PLAN"] for row in cursor.fetchall()),
        )

    def run_query(self, sql: str) -> list:
        """Run a read-only query (SELECT / WITH / VALUES / TABLE) and return its rows as dicts."""
        statement = ensure_single_statement(sql)
        ensure_query_statement(statement)
        return self._run_user_sql(statement, lambda cursor: [dict(row) for row in cursor.fetchall()])

    def benchmark_in_sandbox(self, table_names: list, original_sql: str, suggested_ddl: str):
        """EXPLAIN ANALYZE original_sql against copies of its tables with the suggested indexes applied.

        Needs DATABASE_URL privileges (the read-only role can't create schemas), so everything runs in
        one transaction that is always rolled back: the sandbox schema, the copies and the indexes never
        reach the database. suggested_ddl is LLM output and goes through benchmark_setup_statements.
        """
        statement = ensure_single_statement(original_sql)
        ensure_query_statement(statement)
        setup = benchmark_setup_statements(suggested_ddl or "")
        schema = pgsql.Identifier(f"surgeon_tmp_{uuid.uuid4().hex[:12]}")

        conn = psycopg2.connect(self.dsn, application_name="sql_surgeon_benchmark")
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SET LOCAL statement_timeout = '300s'")  # DDL + EXPLAIN ANALYZE
                cursor.execute("SET LOCAL lock_timeout = %s", (self.config.lock_timeout_ms,))
                cursor.execute(pgsql.SQL("CREATE SCHEMA {}").format(schema))
                for table in table_names:
                    # Lowercased like an unquoted identifier would be
                    t = pgsql.Identifier(table.lower())
                    cursor.execute(pgsql.SQL(
                        "CREATE TABLE {}.{} (LIKE public.{} INCLUDING DEFAULTS INCLUDING INDEXES)"
                    ).format(schema, t, t))
                    cursor.execute(pgsql.SQL("INSERT INTO {}.{} SELECT * FROM public.{} LIMIT 100000").format(schema, t, t))

                cursor.execute(pgsql.SQL("SET LOCAL search_path TO {}").format(schema))
                for ddl in setup:
                    cursor.execute(ddl)

                cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}")
                return cursor.fetchone()["QUERY PLAN"]

        except Exception as e:
            logger.error(f"Error running sandbox benchmark: {e}")
            raise e

        finally:
            # Never commit: rolling back discards the sandbox schema and anything the DDL did
            try:
                conn.rollback()
            finally:
                conn.close()
