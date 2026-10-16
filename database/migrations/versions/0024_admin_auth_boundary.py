"""Admin authentication boundary: the runtime roles can no longer create or change an administrator's identity,
credentials, privileges or sessions (security review of KYC PR4, 2026-10-16).

Revision ID: 0024
Revises: 0023

Before this, hyperchat_app (the API's general database role) wrote users, sessions, user_identities, user_invites and
password_reset_tokens freely, so its credentials alone (a SQL injection anywhere in the API, a leaked connection)
could overwrite an admin's password hash, insert an admin session, promote a user, bind a Google identity to an
admin, forge an admin invite or reset link, and then sign in through the real API.

Now:
* hyperchat_auth (deploy/postgres/roles.sql) is the role of the narrow authentication code path (app.auth's
  sign-in, session issuance, password change / reset, invites, Google linking; app.admin's user management; the
  bootstrap CLI). It gets exactly the table privileges those flows need, nothing else.
* Guard triggers refuse, for the runtime roles (hyperchat_app, hyperchat_worker), every write that could create or
  change admin authority:
    users                  insert with role 'admin'; any update of an admin row (except unlinking its creator,
                           streamer_id -> NULL) or making a row admin; deleting an admin
    sessions               inserting a session for an admin (under a share lock on the user row, so a concurrent
                           promotion is seen); for every session: changing who / which token it is, reviving a
                           revoked one, or extending one that already expired (sliding a live one within its
                           absolute expiry stays allowed: the API does that on every request)
    user_identities        linking an identity to an admin; changing any identity's owner, provider or subject;
                           any update of an admin's identity
    password_reset_tokens  creating one for an admin; anything but burning (used_at NULL -> now)
    user_invites           an invite with role 'admin'; anything but accepting or revoking (once)
  The runtime roles keep everything else (ordinary users and creators, revocations, cleanups, deletes).
* No role the API connects as (hyperchat_app, hyperchat_worker, hyperchat_auth) may UPDATE an existing account into
  an admin. hyperchat_auth creates admins only by INSERT (a new-account admin invitation, bootstrap-admin), and both
  are insert-only (no ON CONFLICT DO UPDATE), so credentials, identities or sessions prepared on an existing row
  never come with the privilege, even when that row is inserted concurrently.
* SECURITY DEFINER functions run as the owner and are not the runtime roles; none callable by them writes these tables.

Not covered here (see docs/admin-auth-boundary.md): a compromised API PROCESS can open hyperchat_auth connections
too (peer authentication maps the same OS user); ordinary (non-admin) accounts stay writable by hyperchat_app.

Downgrade drops the triggers and revokes the grants.
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

RUNTIME = "('hyperchat_app', 'hyperchat_worker')"
AUTH_TABLES = ("users", "sessions", "user_identities", "user_invites", "password_reset_tokens")
GUARDS = {  # table -> (trigger function, events)
    "users": ("auth_guard_users", "INSERT OR UPDATE OR DELETE"),
    "sessions": ("auth_guard_sessions", "INSERT OR UPDATE"),
    "user_identities": ("auth_guard_user_identities", "INSERT OR UPDATE"),
    "password_reset_tokens": ("auth_guard_password_reset_tokens", "INSERT OR UPDATE"),
    "user_invites": ("auth_guard_user_invites", "INSERT OR UPDATE"),
}
API_ROLES = "('hyperchat_app', 'hyperchat_worker', 'hyperchat_auth')"
NO_PROMOTION = ("RAISE EXCEPTION 'an existing account cannot be made an administrator' "
                "USING ERRCODE = 'insufficient_privilege';")
REFUSE = ("RAISE EXCEPTION 'administrator credentials, privileges and sessions are written only by the "
          "authentication role' USING ERRCODE = 'insufficient_privilege';")


def _admin(user_expr: str) -> str:
    """The user is an admin, read under a share lock taken BEFORE the role is looked at, so a concurrent promotion of
    that user is waited for and its committed role is the one checked."""
    return f"COALESCE((SELECT u.role FROM public.users u WHERE u.id = {user_expr} FOR SHARE OF u) = 'admin', false)"


def _fn(name: str, body: str, every_api_role: str = "") -> str:
    """`body` runs for the runtime roles only; `every_api_role` first, for hyperchat_auth too."""
    return f"""
        CREATE FUNCTION public.{name}() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
{every_api_role}
            IF current_user NOT IN {RUNTIME} THEN
                RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
            END IF;
{body}
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END $$
    """


def upgrade() -> None:
    op.execute(_fn("auth_guard_users", f"""
            IF (TG_OP = 'INSERT' AND NEW.role = 'admin')
               OR (TG_OP = 'UPDATE' AND NEW.role = 'admin' AND OLD.role <> 'admin')
               OR (TG_OP = 'DELETE' AND OLD.role = 'admin')
               OR (TG_OP = 'UPDATE' AND OLD.role = 'admin'
                   AND ((to_jsonb(NEW) - 'streamer_id' - 'updated_at') <> (to_jsonb(OLD) - 'streamer_id' - 'updated_at')
                        OR NEW.streamer_id IS NOT NULL AND NEW.streamer_id IS DISTINCT FROM OLD.streamer_id)) THEN
                {REFUSE}
            END IF;""", every_api_role=f"""
            IF TG_OP = 'UPDATE' AND NEW.role = 'admin' AND OLD.role IS DISTINCT FROM 'admin'
               AND current_user IN {API_ROLES} THEN
                {NO_PROMOTION}
            END IF;"""))
    op.execute(_fn("auth_guard_sessions", f"""
            IF TG_OP = 'INSERT' THEN
                IF {_admin('NEW.user_id')} THEN
                    {REFUSE}
                END IF;
            ELSIF (to_jsonb(NEW) - 'last_seen_at' - 'idle_expires_at' - 'revoked_at')
                      <> (to_jsonb(OLD) - 'last_seen_at' - 'idle_expires_at' - 'revoked_at')
                  OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
                  OR (NEW.idle_expires_at IS DISTINCT FROM OLD.idle_expires_at
                      AND (OLD.revoked_at IS NOT NULL OR OLD.idle_expires_at <= now()
                           OR OLD.absolute_expires_at <= now() OR NEW.idle_expires_at > OLD.absolute_expires_at)) THEN
                {REFUSE}
            END IF;"""))
    op.execute(_fn("auth_guard_user_identities", f"""
            IF (TG_OP = 'INSERT' AND {_admin('NEW.user_id')})
               OR (TG_OP = 'UPDATE' AND ((NEW.user_id, NEW.provider, NEW.provider_subject)
                                         IS DISTINCT FROM (OLD.user_id, OLD.provider, OLD.provider_subject)
                                         OR {_admin('OLD.user_id')})) THEN
                {REFUSE}
            END IF;"""))
    op.execute(_fn("auth_guard_password_reset_tokens", f"""
            IF (TG_OP = 'INSERT' AND {_admin('NEW.user_id')})
               OR (TG_OP = 'UPDATE' AND ((to_jsonb(NEW) - 'used_at') <> (to_jsonb(OLD) - 'used_at')
                                         OR OLD.used_at IS NOT NULL AND NEW.used_at IS DISTINCT FROM OLD.used_at
                                         OR NEW.used_at IS NULL AND OLD.used_at IS NULL)) THEN
                {REFUSE}
            END IF;"""))
    op.execute(_fn("auth_guard_user_invites", f"""
            IF (TG_OP = 'INSERT' AND NEW.role = 'admin')
               OR (TG_OP = 'UPDATE' AND (
                   (to_jsonb(NEW) - 'accepted_at' - 'accepted_user_id' - 'revoked_at')
                       <> (to_jsonb(OLD) - 'accepted_at' - 'accepted_user_id' - 'revoked_at')
                   OR (OLD.accepted_at IS NOT NULL AND NEW.accepted_at IS DISTINCT FROM OLD.accepted_at)
                   OR (OLD.accepted_user_id IS NOT NULL
                       AND NEW.accepted_user_id IS DISTINCT FROM OLD.accepted_user_id)
                   OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at))) THEN
                {REFUSE}
            END IF;"""))
    for table, (fn, events) in GUARDS.items():
        op.execute(f"REVOKE ALL ON FUNCTION public.{fn}() FROM PUBLIC")
        # "aaa_" sorts first: BEFORE triggers fire in name order, so the guard sees the row before set_updated_at.
        op.execute(f"CREATE TRIGGER aaa_{fn} BEFORE {events} ON public.{table} "
                   f"FOR EACH ROW EXECUTE FUNCTION public.{fn}()")

    # hyperchat_auth: exactly what app.auth / app.admin's credential flows need (deploy/postgres/roles.sql).
    op.execute("GRANT USAGE ON SCHEMA public TO hyperchat_auth")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {', '.join('public.' + t for t in AUTH_TABLES)} TO hyperchat_auth")
    op.execute("GRANT DELETE ON public.user_identities TO hyperchat_auth")  # an email correction unlinks Google
    # Accepting an application invite claims its (unclaimed) creator: read the application, lock + set the owner.
    op.execute("GRANT SELECT ON public.user_signups, public.streamers TO hyperchat_auth")
    op.execute("GRANT UPDATE (user_id) ON public.streamers TO hyperchat_auth")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (user_id) ON public.streamers FROM hyperchat_auth")
    op.execute("REVOKE ALL ON public.user_signups, public.streamers FROM hyperchat_auth")
    op.execute(f"REVOKE ALL ON {', '.join('public.' + t for t in AUTH_TABLES)} FROM hyperchat_auth")
    op.execute("REVOKE USAGE ON SCHEMA public FROM hyperchat_auth")
    for table, (fn, _) in GUARDS.items():
        op.execute(f"DROP TRIGGER aaa_{fn} ON public.{table}")
        op.execute(f"DROP FUNCTION public.{fn}()")
