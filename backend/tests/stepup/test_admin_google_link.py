"""S2 review: an administrator's Google link across the account lifecycle, and the operator's independent verification
(PostgreSQL, the real API, and a fake Google that signs real RS256 ID tokens).

Policy: an administrator never links Google self-service, and nothing the API shows or records is proof of anything.
The only way is `python -m app.stepup.operator link-google`: the operator verifies the administrator out of band, and
the administrator proves control of the exact Google account through Google's device sign-in for the operator's own
OAuth client; the tool verifies the ID token itself and links its subject (public.admin_link_google_identity, owner
only, migration 0026)."""

import json
import time

import httpx
import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.google import DEVICE_CODE_URL, JWKS_URL, TOKEN_URL, GoogleIdentity, GoogleOAuth
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
OPERATOR_CLIENT = "operator-client.apps.googleusercontent.com"
API_CLIENT = "api-web-client.apps.googleusercontent.com"


# ── a fake Google (device endpoint, token endpoint, JWKS) ───────────────────────

class FakeGoogle:
    def __init__(self, subject: str, email: str, **claims) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.subject, self.email = subject, email
        self.claims = claims  # overrides: aud, iss, iat, email_verified ...
        self.pending, self.error, self.signing_key = 0, None, self.key
        self.device_requests = 0

    def id_token(self) -> str:
        now = int(time.time())
        claims = {"iss": "https://accounts.google.com", "aud": OPERATOR_CLIENT, "sub": self.subject, "email": self.email,
                  "email_verified": True, "iat": now, "exp": now + 3600, **self.claims}
        return jwt.encode(claims, self.signing_key, algorithm="RS256", headers={"kid": "k1"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == DEVICE_CODE_URL:
            self.device_requests += 1
            return httpx.Response(200, json={"device_code": "dev-code", "user_code": "ABCD-EFGH",
                                             "verification_url": "https://www.google.com/device", "interval": 0,
                                             "expires_in": 30})
        if url == TOKEN_URL:
            if self.pending:
                self.pending -= 1
                return httpx.Response(428, json={"error": "authorization_pending"})
            if self.error:
                return httpx.Response(400, json={"error": self.error})
            return httpx.Response(200, json={"id_token": self.id_token(), "access_token": "x"})
        if url == JWKS_URL:
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]})
        return httpx.Response(404)

    def oauth(self) -> GoogleOAuth:
        return GoogleOAuth(OPERATOR_CLIENT, "operator-secret", redirect_uri="",
                           client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


async def no_sleep(_seconds):
    return None


def operator(monkeypatch, *argv, google: FakeGoogle | None = None) -> int:
    from app.stepup.operator import main
    name, port = target()
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", f"host={PG_HOST} port={port} dbname={name} user={FIXTURE_ROLE}")
    return main(list(argv), google=google.oauth() if google else None, sleep=no_sleep)


def link_google(monkeypatch, email: str, google: FakeGoogle, expect: str | None = None, verified_by: str = "video call",
                confirm: str | None = None) -> int:
    return operator(monkeypatch, "link-google", "--email", email, "--expect-google-email", expect or google.email,
                    "--verified-by", verified_by, "--operator", "test-operator",
                    "--confirm-database", confirm or target()[0], google=google)


def sign_in(app, subject: str, email: str):
    """A Google sign-in through the API's service: the user, or the AuthError raised."""
    svc = app.app_state.auth
    svc.google.identity = GoogleIdentity(subject, email, True)
    try:
        return app.portal.call(svc.google_login, "code", "v", "n", IP, None)[0]
    except AuthError as exc:
        return exc


def preexisting_link(email: str, subject: str):
    with fixture_db() as conn:  # an identity linked before S2 (the owner)
        conn.execute("INSERT INTO public.user_identities (user_id, provider, provider_subject, email) "
                     "SELECT id, 'google', %s, %s FROM public.users WHERE email = %s", (subject, email, email))


def identities(user_id: str) -> set[str]:
    with db() as conn:
        return {r[0] for r in conn.execute("SELECT provider_subject FROM public.user_identities WHERE user_id = %s",
                                           (user_id,))}


# ── 1. the forged request of the previous design can no longer exist ────────────

def test_a_compromised_auth_role_cannot_fabricate_anything_that_leads_to_a_link(app):
    email = "gl-forge" + DOMAIN
    aid = make_user(email, role="admin")
    with db() as conn:
        assert conn.execute("SELECT to_regclass('public.admin_identity_link_requests')").fetchone()[0] is None
    for role in ("hyperchat_auth", "hyperchat_app", "hyperchat_lifecycle", "hyperchat_stepup", "hyperchat_worker"):
        for sql, args in (
                ("INSERT INTO public.user_identities (user_id, provider, provider_subject) "
                 "VALUES (%s, 'google', 'ATTACKER-SUB')", (aid,)),
                ("SELECT public.admin_link_google_identity(%s, 'ATTACKER-SUB', 'a@evil.invalid', 'me', 'me', "
                 "'{\"method\": \"google_device_flow\"}')", (email,))):
            with db(role) as conn, pytest.raises(REFUSED):
                conn.execute(sql, args)
    assert identities(aid) == set()
    # A refused sign-in records nothing and hands out no code.
    refused = sign_in(app, "ATTACKER-SUB", email)
    assert isinstance(refused, AdminGoogleNotLinked) and not hasattr(refused, "ref")


def test_the_link_function_itself_requires_the_verified_subject_and_the_verification_record(app):
    email = "gl-fn" + DOMAIN
    make_user(email, role="admin")
    call = "SELECT public.admin_link_google_identity(%s, %s, %s, %s, %s, %s::jsonb)"
    good = json.dumps({"method": "google_device_flow"})
    with fixture_db() as conn:
        for args, expected in (((email, "s", "g@x.invalid", "op", " ", good), "operator_and_verification_required"),
                               ((email, " ", "g@x.invalid", "op", "call", good), "verified_google_subject_required"),
                               ((email, "s", "g@x.invalid", "op", "call", json.dumps({"method": "code"})),
                                "verified_google_subject_required"),
                               (("gl-nobody" + DOMAIN, "s", "g@x.invalid", "op", "call", good), "not_admin")):
            assert conn.execute(call, args).fetchone()[0] == expected
    make_user("gl-fn-user" + DOMAIN, role="user")
    preexisting_link("gl-fn-user" + DOMAIN, "taken-sub")
    with fixture_db() as conn:  # the function's own answers (not the unique key's, not the tool's pre-check)
        assert conn.execute(call, ("gl-fn-user" + DOMAIN, "s2", "g@x.invalid", "op", "call", good)).fetchone()[0] \
            == "not_admin"
        assert conn.execute(call, (email, "taken-sub", "g@x.invalid", "op", "call", good)).fetchone()[0] \
            == "subject_in_use"


# ── 2/3. the operator's independent verification ────────────────────────────────

def test_the_operator_links_exactly_the_google_account_the_admin_proved_control_of(app, monkeypatch):
    email = "gl-ok" + DOMAIN
    aid = make_user(email, role="admin")
    google = FakeGoogle("real-sub", "admin.real@gmail.invalid")
    google.pending = 2  # the administrator takes a moment on their device
    assert link_google(monkeypatch, email, google) == 0
    assert identities(aid) == {"real-sub"} and sign_in(app, "real-sub", email).id == aid
    with fixture_db() as conn:
        event = conn.execute("SELECT operator, detail FROM public.stepup_events WHERE user_id = %s "
                             "AND event = 'identity_linked'", (aid,)).fetchone()
    assert event[0] == "test-operator" and event[1]["verified_by"] == "video call"
    assert event[1]["method"] == "google_device_flow" and event[1]["google_email"] == "admin.real@gmail.invalid"
    assert isinstance(sign_in(app, "another-sub", email), AdminGoogleNotLinked)  # only that subject


@pytest.mark.parametrize("break_it,reason", [
    (lambda g: setattr(g, "email", "someone.else@gmail.invalid"), "wrong google account (email)"),
    (lambda g: g.claims.update(aud=API_CLIENT), "token for the API's client"),
    (lambda g: g.claims.update(iss="https://evil.example"), "wrong issuer"),
    (lambda g: g.claims.update(email_verified=False), "unverified email"),
    (lambda g: g.claims.update(iat=int(time.time()) - 1200), "stale token"),
    (lambda g: setattr(g, "signing_key", rsa.generate_private_key(public_exponent=65537, key_size=2048)),
     "signed by another key"),
    (lambda g: setattr(g, "error", "access_denied"), "the person declined"),
    (lambda g: setattr(g, "error", "expired_token"), "the device code expired"),
])
def test_every_failed_google_verification_links_nothing(app, monkeypatch, break_it, reason):
    email = "gl-bad" + DOMAIN
    aid = make_user(email, role="admin")
    google = FakeGoogle("bad-sub", "admin.real@gmail.invalid")
    expect = google.email
    break_it(google)
    assert link_google(monkeypatch, email, google, expect=expect) == 2, reason
    assert identities(aid) == set()


@pytest.mark.parametrize("error", ["access_denied", "expired_token"])
def test_a_declined_or_expired_google_sign_in_stops_at_once_and_says_why(app, monkeypatch, capsys, error):
    email = "gl-declined" + DOMAIN
    make_user(email, role="admin")
    google = FakeGoogle("declined-sub", "declined@gmail.invalid")
    google.error = error
    started = time.monotonic()
    assert link_google(monkeypatch, email, google) == 2
    assert time.monotonic() - started < 5  # not polled until the device code expires
    assert f"Google verification failed: {error}" in capsys.readouterr().err


def test_the_operator_must_verify_the_admin_and_name_the_database(app, monkeypatch):
    email = "gl-checks" + DOMAIN
    aid = make_user(email, role="admin")
    google = FakeGoogle("checks-sub", "checks@gmail.invalid")
    assert link_google(monkeypatch, email, google, verified_by=" ") == 2
    assert link_google(monkeypatch, email, google, confirm="another-database") == 2
    user = "gl-checks-user" + DOMAIN
    make_user(user, role="user")
    assert link_google(monkeypatch, user, google) == 2  # not an administrator ...
    assert google.device_requests == 0  # ... and nobody was asked to sign in with Google for any of these
    assert identities(aid) == set()


def test_the_operator_never_moves_a_google_account_that_belongs_to_someone_else(app, monkeypatch):
    email = "gl-in-use" + DOMAIN
    make_user(email, role="admin")
    other = make_user("gl-in-use-user" + DOMAIN, role="user")
    preexisting_link("gl-in-use-user" + DOMAIN, "shared-sub")
    assert link_google(monkeypatch, email, FakeGoogle("shared-sub", "shared@gmail.invalid")) == 2
    assert identities(other) == {"shared-sub"}


def test_the_operator_client_is_never_the_api_client(monkeypatch):
    from app.auth.google import GoogleAuthError
    from app.stepup.operator import operator_google
    monkeypatch.setenv("HYPERCHAT_OPERATOR_GOOGLE_CLIENT_ID", API_CLIENT)
    monkeypatch.setenv("HYPERCHAT_OPERATOR_GOOGLE_CLIENT_SECRET", "s")
    monkeypatch.setenv("HYPERCHAT_GOOGLE_CLIENT_ID", API_CLIENT)
    with pytest.raises(GoogleAuthError, match="operator_client_must_not_be_the_api_client"):
        operator_google()
    monkeypatch.delenv("HYPERCHAT_OPERATOR_GOOGLE_CLIENT_SECRET")
    with pytest.raises(GoogleAuthError, match="not_configured"):
        operator_google()


# ── 1/4. the lifecycle ──────────────────────────────────────────────────────────

def test_after_an_admin_email_change_google_never_relinks_itself_only_the_operator_can(app, monkeypatch):
    old, new = "gl-admin-old" + DOMAIN, "gl-admin-new" + DOMAIN
    aid = make_user(old, role="admin")
    preexisting_link(old, "gl-sub")
    assert sign_in(app, "gl-sub", old).id == aid
    boss = new_browser(app)
    headers = signed_in(boss, "gl-boss" + DOMAIN, role="admin")
    r = boss.put(f"/api/v1/admin/users/{aid}/email", headers=headers, json={"email": new})
    assert r.status_code == 200 and r.json()["identities_removed"] == 1 and identities(aid) == set()
    assert sign_in(app, "gl-sub", old).reason == "no_account"  # the old address no longer exists
    assert isinstance(sign_in(app, "gl-sub", new), AdminGoogleNotLinked)  # never re-linked by itself
    assert identities(aid) == set()
    assert link_google(monkeypatch, new, FakeGoogle("gl-sub", "gl.admin@gmail.invalid")) == 0  # operator-verified
    assert sign_in(app, "gl-sub", new).id == aid


def test_passkey_recovery_and_deactivation_keep_a_linked_google_account_working(app):
    email = "gl-recover" + DOMAIN
    aid = make_user(email, role="admin")
    preexisting_link(email, "gl-recover-sub")
    with fixture_db() as conn:
        conn.execute("SELECT public.stepup_recover_admin(%s, 'op', 'lost both phones')", (email,))
    assert sign_in(app, "gl-recover-sub", email).id == aid  # recovery ends sessions and passkeys, not the link
    with db() as conn:
        aal = conn.execute("SELECT public.stepup_session_aal(id) FROM public.sessions WHERE user_id = %s "
                           "AND revoked_at IS NULL", (aid,)).fetchone()[0]
    assert aal == 1  # Google alone never restores passkey assurance
    boss = new_browser(app)
    headers = signed_in(boss, "gl-recover-boss" + DOMAIN, role="admin")
    assert boss.patch(f"/api/v1/admin/users/{aid}", headers=headers, json={"is_active": False}).status_code == 200
    assert isinstance(sign_in(app, "gl-recover-sub", email), AuthError)  # deactivated
    assert boss.patch(f"/api/v1/admin/users/{aid}", headers=headers, json={"is_active": True}).status_code == 200
    assert sign_in(app, "gl-recover-sub", email).id == aid and identities(aid) == {"gl-recover-sub"}


def test_a_new_admin_from_an_invitation_has_no_google_link_and_cannot_create_one(app, monkeypatch):
    boss = new_browser(app)
    headers = signed_in(boss, "gl-inviter" + DOMAIN, role="admin")
    email = "gl-invited" + DOMAIN
    assert boss.post("/api/v1/auth/invites", headers=headers, json={"email": email, "role": "admin"}).status_code == 201
    token = app.app_state.auth.sender.last_token()
    assert app.post("/api/v1/auth/invites/accept", json={"token": token, "password": "Invited-Admin-42"},
                    headers={"origin": "https://hyperchat.space"}).status_code == 200
    assert isinstance(sign_in(app, "gl-invited-sub", email), AdminGoogleNotLinked)
    assert link_google(monkeypatch, email, FakeGoogle("gl-invited-sub", "invited@gmail.invalid")) == 0


def test_promotion_stays_impossible_and_a_demoted_admin_links_like_any_user(app):
    boss = new_browser(app)
    headers = signed_in(boss, "gl-promo-boss" + DOMAIN, role="admin")
    user_email = "gl-promo-user" + DOMAIN
    uid = make_user(user_email, role="user")
    preexisting_link(user_email, "gl-promo-user-sub")  # an ordinary user's Google link
    assert boss.patch(f"/api/v1/admin/users/{uid}", headers=headers, json={"role": "admin"}).status_code == 409
    demoted_email = "gl-demoted" + DOMAIN
    did = make_user(demoted_email, role="admin")
    assert boss.patch(f"/api/v1/admin/users/{did}", headers=headers, json={"role": "streamer"}).status_code == 200
    assert sign_in(app, "gl-demoted-sub", demoted_email).id == did  # no longer an admin: ordinary email linking
