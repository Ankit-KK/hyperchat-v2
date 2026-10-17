"""S2 review: an administrator's Google link across the account lifecycle (PostgreSQL, real API).

Policy: an administrator never links Google self-service (not by email, not after an email change, a passkey
recovery or an invitation). A refused sign-in records a link request (it grants nothing) and shows its code; only the
operator, after confirming out of band, links that exact Google account (public.admin_link_google_identity, 0026).
"""

from urllib.parse import parse_qs, urlparse

import psycopg
import pytest

from app.auth.google import GoogleIdentity
from app.auth.service import AdminGoogleNotLinked, AuthError
from tests.integration import (
    DOMAIN,
    FIXTURE_ROLE,
    PG_HOST,
    db,
    fixture_db,
    make_user,
    new_browser,
    requires_db,
    signed_in,
    target,
)

pytestmark = requires_db
REFUSED = psycopg.errors.InsufficientPrivilege
IP = "203.0.113.20"


def google(app, subject: str, email: str):
    """Sign in with Google through the service; the user, or the AuthError raised."""
    svc = app.app_state.auth
    svc.google.identity = GoogleIdentity(subject, email, True)
    try:
        return app.portal.call(svc.google_login, "code", "v", "n", IP, None)[0]
    except AuthError as exc:
        return exc


def google_callback(app, subject: str, email: str) -> dict:
    """The browser flow: the redirect's query (error and ref)."""
    app.app_state.auth.google.identity = GoogleIdentity(subject, email, True)
    b = new_browser(app)
    r = b.get("/api/v1/auth/google/start", params={"redirect": "/dashboard"}, follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = b.get("/api/v1/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    return {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}


def link(email: str, subject: str):
    with fixture_db() as conn:  # an identity linked before S2, or by the operator
        conn.execute("INSERT INTO public.user_identities (user_id, provider, provider_subject, email) "
                     "SELECT id, 'google', %s, %s FROM public.users WHERE email = %s", (subject, email, email))


def identities(user_id: str) -> set[str]:
    with db() as conn:
        return {r[0] for r in conn.execute("SELECT provider_subject FROM public.user_identities WHERE user_id = %s",
                                           (user_id,))}


def operator(monkeypatch, *argv) -> int:
    from app.stepup.operator import main
    name, port = target()
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", f"host={PG_HOST} port={port} dbname={name} user={FIXTURE_ROLE}")
    return main(list(argv))


def operator_link(monkeypatch, email: str, code: str, verified_by: str = "video call") -> int:
    name, _ = target()
    return operator(monkeypatch, "link-google", "--email", email, "--code", code, "--verified-by", verified_by,
                    "--operator", "test-operator", "--confirm-database", name)


# ── 1. the reported sequence ────────────────────────────────────────────────────

def test_after_an_admin_email_change_google_never_relinks_itself_only_the_operator_can(app, monkeypatch):
    old, new = "gl-admin-old" + DOMAIN, "gl-admin-new" + DOMAIN
    aid = make_user(old, role="admin")
    link(old, "gl-sub")
    assert google(app, "gl-sub", old).id == aid  # linked: signs in
    boss = new_browser(app)
    headers = signed_in(boss, "gl-boss" + DOMAIN, role="admin")
    r = boss.put(f"/api/v1/admin/users/{aid}/email", headers=headers, json={"email": new})
    assert r.status_code == 200 and r.json()["identities_removed"] == 1 and identities(aid) == set()
    # The same Google account: with the old address there is no account any more; with the new one the admin is
    # found but never re-linked by itself (a link request is recorded instead).
    assert google(app, "gl-sub", old).reason == "no_account"
    refused = google(app, "gl-sub", new)
    assert isinstance(refused, AdminGoogleNotLinked) and refused.status == 403 and refused.ref
    assert identities(aid) == set()
    query = google_callback(app, "gl-sub", new)  # the browser sees the reason and the request's code
    assert query["error"] == "admin_google_not_linked" and query["ref"] == refused.ref
    # The operator links exactly that Google account after confirming with the administrator.
    assert operator_link(monkeypatch, new, query["ref"]) == 0
    assert identities(aid) == {"gl-sub"} and google(app, "gl-sub", new).id == aid
    with fixture_db() as conn:
        event = conn.execute("SELECT operator, detail FROM public.stepup_events WHERE user_id = %s "
                             "AND event = 'identity_linked'", (aid,)).fetchone()
    assert event[0] == "test-operator" and event[1]["verified_by"] == "video call"
    # Another Google account with the admin's email is still refused (only the approved subject was linked).
    assert isinstance(google(app, "gl-other-sub", new), AdminGoogleNotLinked)


# ── 2/3. the operator process: narrow, audited, independently authorized ────────

def test_a_link_request_grants_nothing_and_is_decided_once_by_the_operator(app, monkeypatch):
    email = "gl-req" + DOMAIN
    aid = make_user(email, role="admin")
    first = google(app, "gl-req-sub", email)
    assert google(app, "gl-req-sub", email).ref == first.ref  # the same open request, the same code
    name, _ = target()
    assert operator_link(monkeypatch, email, first.ref, verified_by=" ") == 2  # verification is required
    assert operator_link(monkeypatch, "gl-someone-else" + DOMAIN, first.ref) == 2  # the code is THIS admin's
    assert operator(monkeypatch, "link-google", "--email", email, "--code", first.ref, "--verified-by", "call",
                    "--operator", "op", "--confirm-database", "another-db") == 2
    assert operator(monkeypatch, "reject-google", "--email", email, "--code", first.ref, "--reason", "not theirs",
                    "--operator", "op", "--confirm-database", name) == 0
    assert operator_link(monkeypatch, email, first.ref) == 2  # decided: never linked afterwards
    with fixture_db() as conn:  # the function's own answer (the request's guard would also refuse a second decision)
        assert conn.execute("SELECT public.admin_link_google_identity(%s, %s, 'op', 'call')",
                            (email, first.ref)).fetchone()[0] == "decided"
    assert identities(aid) == set()
    second = google(app, "gl-req-sub", email)  # a new attempt is a new request
    assert second.ref != first.ref
    with guards_off() as conn:
        conn.execute("UPDATE public.admin_identity_link_requests SET created_at = now() - interval '2 days', "
                     "expires_at = now() - interval '1 day' WHERE code = %s", (second.ref,))
    assert operator_link(monkeypatch, email, second.ref) == 2  # expired
    assert identities(aid) == set()


def test_the_operator_never_moves_a_google_account_that_belongs_to_someone_else(app, monkeypatch):
    email = "gl-in-use" + DOMAIN
    make_user(email, role="admin")
    other = make_user("gl-in-use-user" + DOMAIN, role="user")
    request = google(app, "gl-shared-sub", email)
    link("gl-in-use-user" + DOMAIN, "gl-shared-sub")
    assert operator_link(monkeypatch, email, request.ref) == 2
    with fixture_db() as conn:  # refused by the function itself (the unique identity key would also stop it)
        assert conn.execute("SELECT public.admin_link_google_identity(%s, %s, 'op', 'call')",
                            (email, request.ref)).fetchone()[0] == "subject_in_use"
    assert identities(other) == {"gl-shared-sub"}


def test_requests_are_bounded_and_only_for_active_admins(app):
    email = "gl-bounded" + DOMAIN
    aid = make_user(email, role="admin")
    refs = {google(app, f"gl-bounded-{i}", email).ref for i in range(4)}
    assert None in refs and len(refs - {None}) == 3  # the fourth open request is not recorded (still refused)
    with fixture_db() as conn:
        assert conn.execute("SELECT count(*) FROM public.admin_identity_link_requests WHERE user_id = %s",
                            (aid,)).fetchone()[0] == 3
    with db("hyperchat_auth") as conn, pytest.raises(REFUSED):  # never for a non-admin
        user = make_user("gl-bounded-user" + DOMAIN, role="user")
        conn.execute("INSERT INTO public.admin_identity_link_requests (id, user_id, provider, provider_subject, code, "
                     "expires_at) VALUES (gen_random_uuid(), %s, 'google', 's', 'AAAAA-AAAAA', now())", (user,))


@pytest.mark.parametrize("role", ["hyperchat_app", "hyperchat_auth", "hyperchat_lifecycle", "hyperchat_stepup"])
def test_no_api_role_can_approve_a_request_or_link_an_admin(app, role):
    email = f"gl-roles-{role[10:]}" + DOMAIN
    aid = make_user(email, role="admin")
    ref = google(app, "gl-roles-sub", email).ref
    attempts = [
        ("SELECT public.admin_link_google_identity(%s, %s, 'me', 'me')", (email, ref)),
        ("SELECT public.admin_reject_google_link(%s, %s, 'me', 'me')", (email, ref)),
        ("UPDATE public.admin_identity_link_requests SET decided_at = now(), decision = 'linked', decided_by = 'me' "
         "WHERE code = %s", (ref,)),
        ("INSERT INTO public.admin_identity_link_requests (id, user_id, provider, provider_subject, code, expires_at, "
         "decided_at, decision, decided_by) VALUES (gen_random_uuid(), %s, 'google', 'x', 'BBBBB-BBBBB', now(), now(), "
         "'linked', 'me')", (aid,)),
        ("INSERT INTO public.user_identities (user_id, provider, provider_subject) VALUES (%s, 'google', 'gl-roles-sub')",
         (aid,)),
        ("DELETE FROM public.admin_identity_link_requests WHERE code = %s", (ref,)),
    ]
    for sql, args in attempts:
        with db(role) as conn, pytest.raises(REFUSED):
            conn.execute(sql, args)
    assert identities(aid) == set()


# ── 4. the analogous lifecycle paths ────────────────────────────────────────────

def test_passkey_recovery_and_deactivation_keep_a_linked_google_account_working(app):
    email = "gl-recover" + DOMAIN
    aid = make_user(email, role="admin")
    link(email, "gl-recover-sub")
    with fixture_db() as conn:
        conn.execute("SELECT public.stepup_recover_admin(%s, 'op', 'lost both phones')", (email,))
    assert google(app, "gl-recover-sub", email).id == aid  # recovery ends sessions and passkeys, not the link
    with db() as conn:
        aal = conn.execute("SELECT public.stepup_session_aal(id) FROM public.sessions WHERE user_id = %s "
                           "AND revoked_at IS NULL", (aid,)).fetchone()[0]
    assert aal == 1  # Google alone never restores passkey assurance
    boss = new_browser(app)
    headers = signed_in(boss, "gl-recover-boss" + DOMAIN, role="admin")
    assert boss.patch(f"/api/v1/admin/users/{aid}", headers=headers, json={"is_active": False}).status_code == 200
    assert isinstance(google(app, "gl-recover-sub", email), AuthError)  # deactivated
    assert boss.patch(f"/api/v1/admin/users/{aid}", headers=headers, json={"is_active": True}).status_code == 200
    assert google(app, "gl-recover-sub", email).id == aid and identities(aid) == {"gl-recover-sub"}


def test_a_new_admin_from_an_invitation_has_no_google_link_and_cannot_create_one(app, monkeypatch):
    boss = new_browser(app)
    headers = signed_in(boss, "gl-inviter" + DOMAIN, role="admin")
    email = "gl-invited" + DOMAIN
    assert boss.post("/api/v1/auth/invites", headers=headers, json={"email": email, "role": "admin"}).status_code == 201
    token = app.app_state.auth.sender.last_token()
    assert app.post("/api/v1/auth/invites/accept", json={"token": token, "password": "Invited-Admin-42"},
                    headers={"origin": "https://hyperchat.space"}).status_code == 200
    refused = google(app, "gl-invited-sub", email)
    assert isinstance(refused, AdminGoogleNotLinked) and refused.ref
    assert operator_link(monkeypatch, email, refused.ref) == 0  # the operator path is the only one


def test_promotion_stays_impossible_and_a_demoted_admin_links_like_any_user(app):
    boss = new_browser(app)
    headers = signed_in(boss, "gl-promo-boss" + DOMAIN, role="admin")
    user_email = "gl-promo-user" + DOMAIN
    uid = make_user(user_email, role="user")
    link(user_email, "gl-promo-user-sub")  # an ordinary user's Google link
    r = boss.patch(f"/api/v1/admin/users/{uid}", headers=headers, json={"role": "admin"})
    assert r.status_code == 409  # migration 0024: no existing account becomes an admin, so no link comes with it
    demoted_email = "gl-demoted" + DOMAIN
    did = make_user(demoted_email, role="admin")
    assert boss.patch(f"/api/v1/admin/users/{did}", headers=headers, json={"role": "streamer"}).status_code == 200
    assert google(app, "gl-demoted-sub", demoted_email).id == did  # no longer an admin: ordinary email linking


# ── helpers ─────────────────────────────────────────────────────────────────────

class guards_off:
    """Owner-only test hook: age a request (its guard forbids changing it otherwise)."""

    def __enter__(self):
        self.conn = fixture_db()
        self.tx = self.conn.transaction()
        self.tx.__enter__()
        self.conn.execute("ALTER TABLE public.admin_identity_link_requests DISABLE TRIGGER USER")
        return self.conn

    def __exit__(self, *exc):
        self.conn.execute("ALTER TABLE public.admin_identity_link_requests ENABLE TRIGGER USER")
        self.tx.__exit__(*exc)
        self.conn.close()
