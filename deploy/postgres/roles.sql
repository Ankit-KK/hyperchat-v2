-- Cluster-level roles for HyperChat. Run once per cluster as the postgres superuser:
--   sudo -u postgres psql -p <port> -v ON_ERROR_STOP=1 -f roles.sql
-- Idempotent. No passwords: every login role authenticates with peer over the Unix socket
-- (see pg_ident.conf). Database objects are owned by hyperchat_owner (NOLOGIN).

DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT * FROM (VALUES
      ('hyperchat_owner',    false),
      ('hyperchat_migrator', true),
      ('hyperchat_app',      true),
      ('hyperchat_worker',   true),
      ('hyperchat_readonly', true),
      ('hyperchat_lifecycle', true)) AS t(name, can_login)
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r.name) THEN
      EXECUTE format('CREATE ROLE %I %s NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
                     r.name, CASE WHEN r.can_login THEN 'LOGIN' ELSE 'NOLOGIN' END);
    END IF;
  END LOOP;
END $$;

-- The migrator may act as the owner (Alembic runs "SET ROLE hyperchat_owner"); nothing else may.
-- NOINHERIT first: on PostgreSQL 16+ a membership grant records the inherit option at grant time.
ALTER ROLE hyperchat_migrator NOINHERIT;
GRANT hyperchat_owner TO hyperchat_migrator;

ALTER ROLE hyperchat_app      CONNECTION LIMIT 20;
ALTER ROLE hyperchat_worker   CONNECTION LIMIT 10;
ALTER ROLE hyperchat_readonly CONNECTION LIMIT 5;
ALTER ROLE hyperchat_lifecycle CONNECTION LIMIT 5;
ALTER ROLE hyperchat_lifecycle SET statement_timeout = '15s';
ALTER ROLE hyperchat_readonly SET default_transaction_read_only = on;
ALTER ROLE hyperchat_app      SET statement_timeout = '15s';
ALTER ROLE hyperchat_worker   SET statement_timeout = '5min';

COMMENT ON ROLE hyperchat_owner    IS 'Owns all HyperChat schema objects. NOLOGIN.';
COMMENT ON ROLE hyperchat_migrator IS 'Runs Alembic migrations (SET ROLE hyperchat_owner).';
COMMENT ON ROLE hyperchat_app      IS 'FastAPI runtime: DML only.';
COMMENT ON ROLE hyperchat_worker   IS 'Background worker runtime: DML only.';
COMMENT ON ROLE hyperchat_readonly IS 'Read-only reporting and verification.';
COMMENT ON ROLE hyperchat_lifecycle IS 'Admin creator status transitions only (EXECUTE on kyc_change_creator_status; no table access).';
