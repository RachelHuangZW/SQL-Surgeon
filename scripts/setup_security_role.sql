-- =============================================================================
-- SQL-Surgeon: least-privilege role for running user-submitted SQL
-- =============================================================================
-- Creates/updates `sql_surgeon_readonly`, the role behind SURGEON_READONLY_DATABASE_URL.
-- It can read tables (SELECT only) so EXPLAIN ANALYZE works, and nothing else.
--
-- Run with psql as a superuser or the database owner, connected to the TARGET database:
--
--   psql "$ADMIN_DATABASE_URL" \
--        -v ro_password='change-me' \
--        -f scripts/setup_security_role.sql
--
-- Optional variables:
--   -v owner_role=app_owner     Role that will CREATE future tables/schemas (default: current_user).
--                               Default privileges only apply to objects created by this role.
--   -v all_schemas=on           Also grant SELECT on future tables in ALL schemas, not just public.
--   -v revoke_from_public=on    Also revoke dblink / postgres_fdw function EXECUTE from PUBLIC
--                               (database-wide; affects every role, read the note in section 6).
--
-- Safe to re-run: every statement is idempotent.
-- =============================================================================

\set ON_ERROR_STOP on

\if :{?ro_password}
\else
  \echo 'ERROR: missing password. Usage: psql ... -v ro_password=<password> -f scripts/setup_security_role.sql'
  \quit
\endif

\if :{?owner_role}
\else
  SELECT current_user AS owner_role \gset
\endif

SELECT current_database() AS target_db \gset

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. Role: login only, no elevated attributes
-- -----------------------------------------------------------------------------
SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sql_surgeon_readonly') AS need_role \gset
\if :need_role
  CREATE ROLE sql_surgeon_readonly;
\endif

ALTER ROLE sql_surgeon_readonly WITH
    LOGIN
    PASSWORD :'ro_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 20;

-- Session defaults: a backstop for any client that connects with these credentials
-- outside the app (the app still sets READ ONLY + SET LOCAL timeouts per transaction).
ALTER ROLE sql_surgeon_readonly SET default_transaction_read_only = on;
ALTER ROLE sql_surgeon_readonly SET statement_timeout = '5s';
ALTER ROLE sql_surgeon_readonly SET lock_timeout = '2s';
ALTER ROLE sql_surgeon_readonly SET idle_in_transaction_session_timeout = '30s';

-- -----------------------------------------------------------------------------
-- 2. Database: CONNECT only (no CREATE schema). TEMP is granted to PUBLIC by default;
--    READ ONLY transactions block CREATE TEMP TABLE regardless.
-- -----------------------------------------------------------------------------
REVOKE ALL ON DATABASE :"target_db" FROM sql_surgeon_readonly;
GRANT CONNECT ON DATABASE :"target_db" TO sql_surgeon_readonly;

-- -----------------------------------------------------------------------------
-- 3. Schema public: USAGE + SELECT on existing and future tables
-- -----------------------------------------------------------------------------
-- Stop PUBLIC from creating objects in schema public (CVE-2018-1058 hardening;
-- already the default on PostgreSQL 15+). The role running the sandbox benchmark
-- (DATABASE_URL) should own the schema or have CREATE granted directly.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

REVOKE ALL ON SCHEMA public FROM sql_surgeon_readonly;
GRANT USAGE ON SCHEMA public TO sql_surgeon_readonly;

-- REVOKE ALL removes INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER (and MAINTAIN on PG17+)
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM sql_surgeon_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO sql_surgeon_readonly;

-- No sequence privileges: nextval()/setval() are writes
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM sql_surgeon_readonly;

ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role" IN SCHEMA public
    GRANT SELECT ON TABLES TO sql_surgeon_readonly;

-- -----------------------------------------------------------------------------
-- 4. Future schemas (opt-in with -v all_schemas=on)
-- -----------------------------------------------------------------------------
-- Covers schemas and tables that :owner_role creates later. Existing non-public
-- schemas are NOT touched; grant those explicitly if the app should see them.
\if :{?all_schemas}
\if :all_schemas
  ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role"
      GRANT USAGE ON SCHEMAS TO sql_surgeon_readonly;
  ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role"
      GRANT SELECT ON TABLES TO sql_surgeon_readonly;
\endif
\endif

-- -----------------------------------------------------------------------------
-- 5. Predefined roles that bypass table privileges or touch the server
-- -----------------------------------------------------------------------------
DO $$
DECLARE
    r text;
BEGIN
    FOR r IN
        SELECT rolname FROM pg_roles
        WHERE rolname IN (
            'pg_write_all_data',          -- DML on every table (PG14+)
            'pg_read_server_files',       -- COPY FROM file, pg_read_file
            'pg_write_server_files',      -- COPY TO file
            'pg_execute_server_program',  -- COPY ... PROGRAM (shell access)
            'pg_signal_backend',          -- pg_terminate_backend on other sessions
            'pg_checkpoint'               -- CHECKPOINT (PG15+)
        )
        AND pg_has_role('sql_surgeon_readonly', oid, 'MEMBER')
    LOOP
        EXECUTE format('REVOKE %I FROM sql_surgeon_readonly', r);
        RAISE NOTICE 'Revoked membership in %', r;
    END LOOP;
END
$$;

-- -----------------------------------------------------------------------------
-- 6. Sensitive functions
-- -----------------------------------------------------------------------------
-- NOTE: privileges granted to PUBLIC apply to every role, and a per-role REVOKE
-- cannot take them away. The REVOKE below is effective for functions that were
-- granted to this role directly (e.g. file access functions an admin granted
-- earlier); for PUBLIC-executable functions like dblink, use revoke_from_public.
DO $$
DECLARE
    f regprocedure;
BEGIN
    FOR f IN
        SELECT p.oid::regprocedure FROM pg_proc p
        WHERE p.proname IN (
            'pg_read_file', 'pg_read_binary_file', 'pg_ls_dir', 'pg_stat_file',
            'lo_import', 'lo_export',
            'pg_terminate_backend', 'pg_cancel_backend',
            'pg_reload_conf', 'pg_rotate_logfile',
            'dblink', 'dblink_exec', 'dblink_connect', 'dblink_connect_u',
            'dblink_send_query', 'dblink_open'
        )
    LOOP
        EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM sql_surgeon_readonly', f);
    END LOOP;
END
$$;

-- dblink can open a second connection with different credentials and write through it,
-- escaping the READ ONLY transaction. Revoking from PUBLIC blocks this for every role
-- that relies on the default grant, so it is opt-in.
\if :{?revoke_from_public}
\if :revoke_from_public
DO $$
DECLARE
    f regprocedure;
BEGIN
    FOR f IN
        SELECT p.oid::regprocedure FROM pg_proc p
        WHERE p.proname LIKE 'dblink%'
           OR p.proname IN ('postgres_fdw_disconnect', 'postgres_fdw_disconnect_all')
    LOOP
        EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC', f);
    END LOOP;
END
$$;
\endif
\endif

COMMIT;

-- -----------------------------------------------------------------------------
-- 7. Verification (expected: t, f, f, f, f)
-- -----------------------------------------------------------------------------
\echo 'Verifying sql_surgeon_readonly privileges:'
SELECT
    bool_and(has_table_privilege('sql_surgeon_readonly', c.oid, 'SELECT'))  AS can_select_all_public,
    bool_or(has_table_privilege('sql_surgeon_readonly', c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE')) AS can_write_any_public,
    has_schema_privilege('sql_surgeon_readonly', 'public', 'CREATE')         AS can_create_in_public,
    has_database_privilege('sql_surgeon_readonly', current_database(), 'CREATE') AS can_create_schema,
    (SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls
       FROM pg_roles WHERE rolname = 'sql_surgeon_readonly')               AS has_elevated_attrs
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'p');
