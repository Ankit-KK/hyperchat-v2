-- D1 auth freeze removal: PROPOSED; rehearsed on a local Supabase stack only (the dashboard role `postgres` could run
-- it there; plain PostgreSQL needs ownership of auth.users to drop its trigger). Run only after the D1 import and the
-- switch to V2 (or to abort before --apply; after --apply, read the abort rules in README.md first). Idempotent.
BEGIN;
SET LOCAL lock_timeout = '10s';
DROP TRIGGER IF EXISTS hyperchat_d1_auth_freeze ON auth.users;
DROP FUNCTION IF EXISTS hyperchat_ops.d1_auth_freeze_guard();
COMMIT;
-- The hyperchat_ops schema is left in place (it may hold nothing else; drop it by hand if empty).
