-- D1 auth freeze: PROPOSED; rehearsed on a local Supabase stack only (GoTrue v2.197.0, PostgreSQL 17.11), not yet on
-- a hosted project. Do not run against any live Supabase project.
--
-- Running it on the legacy production project needs the hosted checks in README.md ("Rehearsal results") and the
-- owner's explicit approval for the maintenance window.
--
-- While installed, every write that could change the reviewed D1 import set is rejected on auth.users:
--   * INSERT (new accounts) and DELETE (removed accounts);
--   * UPDATE of id, email, encrypted_password, deleted_at or banned_until (the fields the importer matches on or
--     decides eligibility from).
-- Every other UPDATE passes, e.g. what an ordinary sign-in or token refresh writes (both worked in the rehearsal), and
-- the recovery-token columns written when a reset email is requested (completing that reset is rejected). GoTrue
-- answers a rejected request with HTTP 500 carrying the trigger's message (see README.md).
--
-- Idempotent: CREATE OR REPLACE everywhere, in one transaction; an ENABLE ALWAYS trigger stays ALWAYS (below).
-- Creating the trigger takes a SHARE ROW EXCLUSIVE lock on auth.users, which waits for every in-flight insert /
-- update / delete on that table to commit or roll back: when this script commits, no earlier write can still land
-- (the drain). lock_timeout bounds the wait, because ordinary sign-ins and token refreshes (which update auth.users)
-- queue behind it; on timeout nothing is installed and the script can be re-run.
--
-- Not a defence against privileged operators: the table owner or a superuser can drop or disable the trigger,
-- TRUNCATE the table (no row triggers fire), or write with session_replication_role = replica unless the trigger is
-- ENABLE ALWAYS. On the local Supabase stack the dashboard role `postgres` (not a superuser, not the owner) could
-- also set session_replication_role = replica and so write past an 'O' trigger, and it cannot make the trigger
-- ALWAYS: that bypass stays open to anyone with the database password (README.md, operator controls). verify.sql and
-- the importer's freeze check prove the trigger is in place, not that nobody with those privileges bypassed it.

BEGIN;
SET LOCAL lock_timeout = '10s';

CREATE SCHEMA IF NOT EXISTS hyperchat_ops;

CREATE OR REPLACE FUNCTION hyperchat_ops.d1_auth_freeze_guard() RETURNS trigger
LANGUAGE plpgsql AS $freeze$
BEGIN
  IF TG_OP = 'INSERT' THEN
    RAISE EXCEPTION 'hyperchat_d1_freeze: account creation is paused for maintenance' USING ERRCODE = 'P0001';
  ELSIF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'hyperchat_d1_freeze: account deletion is paused for maintenance' USING ERRCODE = 'P0001';
  ELSIF NEW.encrypted_password IS DISTINCT FROM OLD.encrypted_password THEN
    RAISE EXCEPTION 'hyperchat_d1_freeze: password changes are paused for maintenance' USING ERRCODE = 'P0001';
  ELSIF NEW.email IS DISTINCT FROM OLD.email THEN
    RAISE EXCEPTION 'hyperchat_d1_freeze: email changes are paused for maintenance' USING ERRCODE = 'P0001';
  ELSIF NEW.id IS DISTINCT FROM OLD.id
     OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
     OR NEW.banned_until IS DISTINCT FROM OLD.banned_until THEN
    RAISE EXCEPTION 'hyperchat_d1_freeze: account status changes are paused for maintenance' USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END;
$freeze$;

-- CREATE OR REPLACE TRIGGER resets an ENABLE ALWAYS trigger to plain enabled ('O'), which would silently reopen the
-- replica-mode path (found in the rehearsal). Remember the state here and restore it below.
DO $state$
BEGIN
  PERFORM set_config('hyperchat_ops.freeze_was_always', (EXISTS (
    SELECT 1 FROM pg_catalog.pg_trigger
    WHERE tgrelid = 'auth.users'::regclass AND tgname = 'hyperchat_d1_auth_freeze' AND tgenabled = 'A'))::text, true);
END
$state$;

CREATE OR REPLACE TRIGGER hyperchat_d1_auth_freeze
  BEFORE INSERT OR UPDATE OR DELETE ON auth.users
  FOR EACH ROW EXECUTE FUNCTION hyperchat_ops.d1_auth_freeze_guard();

-- Restoring ALWAYS needs ownership of auth.users. Without it this raises and the whole script rolls back, leaving the
-- earlier ENABLE ALWAYS trigger exactly as it was: a re-run keeps ALWAYS or changes nothing, never downgrades it.
-- A disabled ('D') or replica-only ('R') trigger is re-armed as plain enabled ('O').
DO $always$
BEGIN
  IF current_setting('hyperchat_ops.freeze_was_always') = 'true' THEN
    ALTER TABLE auth.users ENABLE ALWAYS TRIGGER hyperchat_d1_auth_freeze;
  END IF;
END
$always$;

COMMIT;

-- Optional, only for the OWNER of auth.users (or a superuser): make the trigger fire even for sessions with
-- session_replication_role = replica. In the local Supabase rehearsal the dashboard role `postgres` could not
-- (42501 must be owner of table users), and could itself write in replica mode past an 'O' trigger; see README.md.
-- The importer's freeze check accepts both 'O' (enabled) and 'A' (always).
-- ALTER TABLE auth.users ENABLE ALWAYS TRIGGER hyperchat_d1_auth_freeze;
