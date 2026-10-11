-- D1 auth freeze: PROPOSED, NOT YET REHEARSED. Do not run against any live Supabase project.
--
-- Rehearse first in a disposable local or staging Supabase project (docs/operations/d1-auth-freeze/README.md);
-- running it on the legacy production project needs the owner's explicit approval for the maintenance window.
--
-- While installed, every write that could change the reviewed D1 import set is rejected on auth.users:
--   * INSERT (new accounts) and DELETE (removed accounts);
--   * UPDATE of id, email, encrypted_password, deleted_at or banned_until (the fields the importer matches on or
--     decides eligibility from).
-- Every other UPDATE passes, e.g. last_sign_in_at / updated_at written by an ordinary sign-in, and the recovery-token
-- columns written when a reset email is requested (completing that reset is rejected). Token refresh and sessions
-- live in other tables and are not touched.
--
-- Idempotent: CREATE OR REPLACE everywhere, in one transaction. Creating the trigger takes a SHARE ROW EXCLUSIVE lock
-- on auth.users, which waits for every in-flight insert / update / delete on that table to commit or roll back: when
-- this script commits, no earlier write can still land (the drain). lock_timeout bounds the wait, because ordinary
-- sign-ins (which update auth.users) queue behind it; on timeout nothing is installed and the script can be re-run.
--
-- Not a defence against privileged operators: the table owner or a superuser can drop or disable the trigger,
-- TRUNCATE the table (no row triggers fire), or write with session_replication_role = replica if the trigger is not
-- ENABLE ALWAYS. verify.sql and the importer's freeze check prove the trigger is in place, not that nobody with those
-- privileges bypassed it.

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

CREATE OR REPLACE TRIGGER hyperchat_d1_auth_freeze
  BEFORE INSERT OR UPDATE OR DELETE ON auth.users
  FOR EACH ROW EXECUTE FUNCTION hyperchat_ops.d1_auth_freeze_guard();

COMMIT;

-- Optional, if the role running this owns auth.users (unverified on Supabase): make the trigger fire even for sessions
-- with session_replication_role = replica. The importer's freeze check accepts both 'O' (enabled) and 'A' (always).
-- ALTER TABLE auth.users ENABLE ALWAYS TRIGGER hyperchat_d1_auth_freeze;
