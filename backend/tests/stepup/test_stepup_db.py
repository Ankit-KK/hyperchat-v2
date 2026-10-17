"""S1 step-up authentication against real PostgreSQL (migration 0025) with real WebAuthn signatures (a software
authenticator): what the verifier accepts and refuses, what each database role can and cannot do, and the one-time
grant's bindings, expiry, replay and concurrency. Synthetic @authtest.invalid administrators only."""

import asyncio
import secrets
import uuid
from contextlib import asynccontextmanager, contextmanager

import psycopg
import pytest

from app.stepup.config import StepupSettings
from app.stepup.verifier import StepupError, Verifier
from tests.integration import DOMAIN, FIXTURE_ROLE, PG_HOST, db, fixture_db, make_user, requires_db, target
from tests.stepup.authenticator import SoftAuthenticator

pytestmark = requires_db
RP, ORIGIN = "hyperchat.test", "https://admin.hyperchat.test"
REFUSED = psycopg.errors.InsufficientPrivilege
API_ROLES = ("hyperchat_app", "hyperchat_auth", "hyperchat_lifecycle", "hyperchat_worker")
ACTIVATE = {"action": "creator.activate", "target": "creator:3f2b9a10-0c1d-4e5f-9a8b-7c6d5e4f3a2b",
            "params": {"expected_status": "pending"}}


# ── fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture
def verifier(env):
    name, port = target()
    settings = StepupSettings(webauthn_rp_id=RP, webauthn_origins=ORIGIN, db_host=PG_HOST, db_port=int(port),
                              db_name=name)

    @asynccontextmanager
    async def connect():
        async with await psycopg.AsyncConnection.connect(settings.conninfo(), autocommit=True) as conn:
            yield conn
    return Verifier(settings, connect)


def run(coro):
    return asyncio.run(coro)


def refusal(coro) -> tuple[int, str]:
    with pytest.raises(StepupError) as exc:
        run(coro)
    return exc.value.status, exc.value.code


def admin(name: str, role: str = "admin") -> tuple[str, str]:
    """(user id, session id): an administrator with one live session."""
    uid = make_user(name + DOMAIN, role=role)
    with fixture_db() as conn:
        sid = conn.execute("INSERT INTO public.sessions (user_id, token_hash, csrf_token_hash, idle_expires_at, "
                           "absolute_expires_at) VALUES (%s, %s, 'x', now() + interval '1 day', "
                           "now() + interval '2 days') RETURNING id::text", (uid, secrets.token_hex(32))).fetchone()[0]
    return uid, sid


def register(v: Verifier, uid: str, sid: str, auth: SoftAuthenticator | None = None) -> tuple[SoftAuthenticator, str]:
    auth = auth or SoftAuthenticator(RP, ORIGIN)
    options = run(v.registration_options(uid, sid))
    result = run(v.registration_verify(uid, sid, options["challenge_id"], auth.register(options["public_key"])))
    assert result["status"] == "pending"
    return auth, result["fingerprint"]


def approve(email: str, fingerprint: str) -> str:
    with fixture_db() as conn:
        return conn.execute("SELECT public.stepup_approve_credential(%s, %s, 'test-operator', 'test: in person')",
                            (email, fingerprint)).fetchone()[0]


def enrolled(v: Verifier, name: str) -> tuple[str, str, SoftAuthenticator]:
    uid, sid = admin(name)
    auth, fp = register(v, uid, sid)
    assert approve(name + DOMAIN, fp) == "ok"
    return uid, sid, auth


def grant(v: Verifier, uid: str, sid: str, auth: SoftAuthenticator, spec: dict = ACTIVATE) -> dict:
    options = run(v.assertion_options(uid, sid, spec["action"], spec["target"], spec["params"]))
    return run(v.assertion_verify(uid, sid, options["challenge_id"], auth.assert_(options["public_key"])))


def consume(role: str, g: dict, uid: str, sid: str, **override) -> str:
    args = {"grant": g["grant_id"], "user": uid, "session": sid, "action": g["action"], "target": g["target"],
            "params_hash": g["params_hash"], **override}
    with db(role) as conn:
        return conn.execute("SELECT public.stepup_consume(%(grant)s, %(user)s, %(session)s, %(action)s, %(target)s, "
                            "%(params_hash)s)", args).fetchone()[0]


@contextmanager
def guards_off(table: str):
    """Owner-only test hook: change a row the guards protect (e.g. age it) inside one transaction."""
    with fixture_db() as conn, conn.transaction():
        conn.execute(f"ALTER TABLE public.{table} DISABLE TRIGGER USER")
        yield conn
        conn.execute(f"ALTER TABLE public.{table} ENABLE TRIGGER USER")


# ── registration ────────────────────────────────────────────────────────────────

def test_a_registered_passkey_is_pending_and_authorizes_nothing_until_approved(verifier):
    uid, sid = admin("su-pending")
    auth, fingerprint = register(verifier, uid, sid)
    with db("hyperchat_stepup") as conn:
        row = conn.execute("SELECT status, fingerprint FROM public.webauthn_credentials WHERE user_id = %s",
                           (uid,)).fetchone()
    assert row == ("pending", fingerprint)
    assert refusal(verifier.assertion_options(uid, sid, **ACTIVATE)) == (409, "no_active_credential")
    assert approve("su-pending" + DOMAIN, fingerprint) == "ok"
    assert "grant_id" in grant(verifier, uid, sid, auth)


@pytest.mark.parametrize("tamper,code", [
    ({"origin": "https://evil.example"}, "verification_failed"),
    ({"origin": "https://admin.hyperchat.test.evil.example"}, "verification_failed"),
    ({"rp_id": "evil.example"}, "verification_failed"),
    ({"uv": False}, "verification_failed"),
    ({"challenge": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}, "verification_failed"),
    ({"client_extra": {"crossOrigin": True}}, "cross_origin"),
    ({"client_extra": {"topOrigin": "https://evil.example"}}, "cross_origin"),
])
def test_registration_refuses_a_wrong_origin_rp_id_challenge_or_missing_user_verification(verifier, tamper, code):
    uid, sid = admin("su-reg-bad")
    options = run(verifier.registration_options(uid, sid))
    bad = SoftAuthenticator(RP, ORIGIN).register(options["public_key"], **tamper)
    assert refusal(verifier.registration_verify(uid, sid, options["challenge_id"], bad))[1] == code
    good = SoftAuthenticator(RP, ORIGIN).register(options["public_key"])  # the challenge is burned now
    assert refusal(verifier.registration_verify(uid, sid, options["challenge_id"], good)) == (409, "challenge_used")
    with db("hyperchat_stepup") as conn:
        assert conn.execute("SELECT count(*) FROM public.webauthn_credentials WHERE user_id = %s", (uid,)).fetchone()[0] == 0


def test_only_an_active_administrators_live_session_can_start_a_ceremony(verifier):
    uid, sid = admin("su-user", role="user")
    assert refusal(verifier.registration_options(uid, sid)) == (403, "not_admin")
    aid, asid = admin("su-revoked-session")
    with fixture_db() as conn:
        conn.execute("UPDATE public.sessions SET revoked_at = now() WHERE id = %s", (asid,))
    assert refusal(verifier.registration_options(aid, asid)) == (403, "session_invalid")
    _, other_sid = admin("su-other-session")
    assert refusal(verifier.registration_options(aid, other_sid)) == (403, "session_invalid")  # not this admin's


# ── assertions and grants ───────────────────────────────────────────────────────

def test_a_grant_is_bound_to_admin_session_action_target_and_parameters_and_is_single_use(verifier):
    uid, sid, auth = enrolled(verifier, "su-bind")
    other_uid, other_sid, _ = enrolled(verifier, "su-bind-other")
    g = grant(verifier, uid, sid, auth)
    for override in ({"user": other_uid}, {"session": other_sid}, {"action": "creator.reactivate"},
                     {"target": "creator:other"}, {"params_hash": "0" * 64}):
        assert consume("hyperchat_lifecycle", g, uid, sid, **override) == "mismatch", override
    assert consume("hyperchat_app", g, uid, sid, grant=str(uuid.uuid4())) == "not_found"
    assert consume("hyperchat_lifecycle", g, uid, sid) == "ok"
    assert consume("hyperchat_lifecycle", g, uid, sid) == "consumed"  # never twice, by any role
    assert consume("hyperchat_auth", g, uid, sid) == "consumed"


def test_a_grant_expires_and_a_consumption_is_only_visible_to_its_own_transaction(verifier):
    uid, sid, auth = enrolled(verifier, "su-expire")
    g = grant(verifier, uid, sid, auth)
    with guards_off("stepup_grants") as conn:
        conn.execute("UPDATE public.stepup_grants SET created_at = now() - interval '3 minutes', "
                     "expires_at = now() - interval '1 minute' WHERE id = %s", (g["grant_id"],))
    assert consume("hyperchat_app", g, uid, sid) == "expired"
    g2 = grant(verifier, uid, sid, auth)
    with fixture_db() as conn, conn.transaction():  # S3's guards ask: consumed in THIS transaction?
        here = "SELECT public.stepup_consumed_here(%s, %s, %s, %s)"
        args = (uid, g2["action"], g2["target"], g2["params_hash"])
        assert conn.execute(here, args).fetchone()[0] is False
        assert conn.execute("SELECT public.stepup_consume(%s, %s, %s, %s, %s, %s)",
                            (g2["grant_id"], uid, sid, *args[1:])).fetchone()[0] == "ok"
        assert conn.execute(here, args).fetchone()[0] is True
    with fixture_db() as conn:
        assert conn.execute(here, args).fetchone()[0] is False  # another transaction: nothing consumed here


def test_two_concurrent_consumptions_of_one_grant_succeed_exactly_once(verifier):
    uid, sid, auth = enrolled(verifier, "su-race-consume")
    g = grant(verifier, uid, sid, auth)
    first = db("hyperchat_app")
    first.autocommit = False
    try:
        assert first.execute("SELECT public.stepup_consume(%s, %s, %s, %s, %s, %s)",
                             (g["grant_id"], uid, sid, g["action"], g["target"], g["params_hash"])).fetchone()[0] == "ok"

        async def second():  # waits on the grant's row lock, then sees it consumed
            return await asyncio.to_thread(consume, "hyperchat_auth", g, uid, sid)

        async def scenario():
            task = asyncio.create_task(second())
            await asyncio.sleep(0.3)
            assert not task.done()
            await asyncio.to_thread(first.commit)
            return await asyncio.wait_for(task, 10)
        assert run(scenario()) == "consumed"
    finally:
        first.close()


def test_a_challenge_is_single_use_and_concurrent_verifications_yield_one_grant(verifier):
    uid, sid, auth = enrolled(verifier, "su-replay")
    options = run(verifier.assertion_options(uid, sid, **ACTIVATE))
    response = auth.assert_(options["public_key"])

    async def both():
        return await asyncio.gather(*(verifier.assertion_verify(uid, sid, options["challenge_id"], response)
                                      for _ in range(2)), return_exceptions=True)
    outcomes = run(both())
    grants = [o for o in outcomes if isinstance(o, dict)]
    refused = [o.code for o in outcomes if isinstance(o, StepupError)]
    assert len(grants) == 1 and refused == ["challenge_used"]
    assert refusal(verifier.assertion_verify(uid, sid, options["challenge_id"], response)) == (409, "challenge_used")
    with db("hyperchat_stepup") as conn:
        assert conn.execute("SELECT count(*) FROM public.stepup_grants WHERE user_id = %s", (uid,)).fetchone()[0] == 1


def test_an_expired_challenge_is_refused(verifier):
    uid, sid, auth = enrolled(verifier, "su-old-challenge")
    options = run(verifier.assertion_options(uid, sid, **ACTIVATE))
    with guards_off("stepup_challenges") as conn:
        conn.execute("UPDATE public.stepup_challenges SET created_at = now() - interval '10 minutes', "
                     "expires_at = now() - interval '6 minutes' WHERE id = %s", (options["challenge_id"],))
    response = auth.assert_(options["public_key"])
    assert refusal(verifier.assertion_verify(uid, sid, options["challenge_id"], response)) == (409, "challenge_expired")


def test_a_signature_over_another_actions_challenge_is_refused(verifier):
    """The browser asked to approve creator B; the response is submitted for creator A's challenge."""
    uid, sid, auth = enrolled(verifier, "su-swap")
    for_a = run(verifier.assertion_options(uid, sid, **ACTIVATE))
    for_b = run(verifier.assertion_options(uid, sid, **{**ACTIVATE, "target": "creator:other"}))
    signed_b = auth.assert_(for_b["public_key"])
    assert refusal(verifier.assertion_verify(uid, sid, for_a["challenge_id"], signed_b)) == (400, "verification_failed")


@pytest.mark.parametrize("tamper,code", [
    ({"origin": "https://evil.example"}, "verification_failed"),
    ({"rp_id": "evil.example"}, "verification_failed"),
    ({"uv": False}, "verification_failed"),
    ({"client_extra": {"crossOrigin": True}}, "cross_origin"),
    ({"sign_count": 0}, "verification_failed"),  # after a counter of 1 was recorded at approval-time use
])
def test_assertions_refuse_wrong_origin_rp_id_no_user_verification_cross_origin_and_counter_regression(
        verifier, tamper, code):
    uid, sid, auth = enrolled(verifier, "su-assert-bad")
    grant(verifier, uid, sid, auth)  # sign count is now 1
    options = run(verifier.assertion_options(uid, sid, **ACTIVATE))
    assert refusal(verifier.assertion_verify(uid, sid, options["challenge_id"],
                                             auth.assert_(options["public_key"], **tamper)))[1] == code
    with db("hyperchat_stepup") as conn:
        assert conn.execute("SELECT outcome FROM public.stepup_challenges WHERE id = %s",
                            (options["challenge_id"],)).fetchone()[0] == "failed"


def test_a_signature_by_another_key_for_the_same_credential_id_is_refused(verifier):
    from cryptography.hazmat.primitives.asymmetric import ec
    uid, sid, auth = enrolled(verifier, "su-forged-key")
    options = run(verifier.assertion_options(uid, sid, **ACTIVATE))
    forged = auth.assert_(options["public_key"], key=ec.generate_private_key(ec.SECP256R1()))
    assert refusal(verifier.assertion_verify(uid, sid, options["challenge_id"], forged)) == (400, "verification_failed")


def test_a_pending_passkey_cannot_answer_even_when_another_passkey_is_active(verifier):
    uid, sid, _ = enrolled(verifier, "su-pending-answer")
    pending, _ = register(verifier, uid, sid)  # registered, never approved
    options = run(verifier.assertion_options(uid, sid, **ACTIVATE))
    response = pending.assert_(options["public_key"])
    assert refusal(verifier.assertion_verify(uid, sid, options["challenge_id"], response)) == (403, "credential_pending")
    with db("hyperchat_stepup") as conn:
        assert conn.execute("SELECT count(*) FROM public.stepup_grants WHERE user_id = %s", (uid,)).fetchone()[0] == 0


def test_another_administrators_passkey_cannot_answer_for_this_administrator(verifier):
    uid, sid, _ = enrolled(verifier, "su-mine")
    _, _, other_auth = enrolled(verifier, "su-theirs")
    options = run(verifier.assertion_options(uid, sid, **ACTIVATE))
    response = other_auth.assert_(options["public_key"])
    assert refusal(verifier.assertion_verify(uid, sid, options["challenge_id"], response)) == (400, "unknown_credential")


def test_revocation_and_demotion_end_what_a_passkey_authorized(verifier):
    uid, sid, auth = enrolled(verifier, "su-revoke")
    options = run(verifier.assertion_options(uid, sid, "session.aal2", "session", {}))
    assert run(verifier.assertion_verify(uid, sid, options["challenge_id"], auth.assert_(options["public_key"])))["aal"] == 2
    g = grant(verifier, uid, sid, auth)
    with db("hyperchat_app") as conn:
        assert conn.execute("SELECT public.stepup_session_aal(%s)", (sid,)).fetchone()[0] == 2
    with db("hyperchat_stepup") as conn:
        fp = conn.execute("SELECT fingerprint FROM public.webauthn_credentials WHERE user_id = %s", (uid,)).fetchone()[0]
    with fixture_db() as conn:
        assert conn.execute("SELECT public.stepup_revoke_credential(%s, %s, 'test-operator', 'lost phone')",
                            ("su-revoke" + DOMAIN, fp)).fetchone()[0] == "ok"
    assert consume("hyperchat_app", g, uid, sid) == "credential_revoked"
    with db("hyperchat_app") as conn:
        assert conn.execute("SELECT public.stepup_session_aal(%s)", (sid,)).fetchone()[0] == 1
    assert refusal(verifier.assertion_options(uid, sid, **ACTIVATE)) == (409, "no_active_credential")
    # Demotion: a grant issued before it is worthless, and no new ceremony starts.
    uid2, sid2, auth2 = enrolled(verifier, "su-demote")
    g2 = grant(verifier, uid2, sid2, auth2)
    with fixture_db() as conn:
        conn.execute("UPDATE public.users SET role = 'streamer' WHERE id = %s", (uid2,))
    assert consume("hyperchat_lifecycle", g2, uid2, sid2) == "not_admin"
    assert refusal(verifier.assertion_options(uid2, sid2, **ACTIVATE)) == (403, "not_admin")


def test_session_assurance_holds_only_for_its_own_live_session(verifier):
    uid, sid, auth = enrolled(verifier, "su-aal")
    _, other_sid = admin("su-aal-other")
    options = run(verifier.assertion_options(uid, sid, "session.aal2", "session", {}))
    run(verifier.assertion_verify(uid, sid, options["challenge_id"], auth.assert_(options["public_key"])))
    aal = "SELECT public.stepup_session_aal(%s)"
    with db("hyperchat_auth") as conn:
        assert conn.execute(aal, (sid,)).fetchone()[0] == 2 and conn.execute(aal, (other_sid,)).fetchone()[0] == 1
    with fixture_db() as conn:
        conn.execute("UPDATE public.sessions SET revoked_at = now() WHERE id = %s", (sid,))
    with db("hyperchat_auth") as conn:
        assert conn.execute(aal, (sid,)).fetchone()[0] == 1


def test_assertion_attempts_are_rate_limited_per_administrator(verifier):
    uid, sid, _ = enrolled(verifier, "su-limit")
    codes = [refusal(verifier.assertion_verify(uid, sid, "00000000-0000-4000-8000-000000000000", {}))[1]
             for _ in range(11)]
    assert codes[:10] == ["challenge_not_found"] * 10 and codes[10] == "rate_limited"
    with db("hyperchat_stepup") as conn:
        assert conn.execute("SELECT count(*) FROM public.stepup_events WHERE user_id = %s AND event = 'rate_limited'",
                            (uid,)).fetchone()[0] == 1


# ── what a compromised API role (or the verifier's own role) cannot do ─────────

@pytest.mark.parametrize("role", API_ROLES)
def test_a_compromised_api_role_cannot_touch_passkeys_grants_assurance_enforcement_or_audit(verifier, role):
    uid, sid, _ = enrolled(verifier, f"su-{role[10:]}")
    with db("hyperchat_stepup") as conn:
        cred, challenge = conn.execute("SELECT c.id, ch.id FROM public.webauthn_credentials c, public.stepup_challenges ch "
                                       "WHERE c.user_id = %s AND ch.user_id = %s LIMIT 1", (uid, uid)).fetchone()
    attempts = [
        ("INSERT INTO public.webauthn_credentials (user_id, credential_id, public_key, fingerprint, status, "
         "approved_at, approved_by) VALUES (%s, %s, %s, 'AAAA-AAAA-AAAA-AAAA-AAAA', 'active', now(), 'me')",
         (uid, secrets.token_bytes(32), secrets.token_bytes(77))),
        ("UPDATE public.webauthn_credentials SET status = 'revoked', revoked_at = now(), revoked_by = 'x', "
         "revoke_reason = 'x' WHERE id = %s", (cred,)),
        ("SELECT public_key FROM public.webauthn_credentials WHERE id = %s", (cred,)),
        ("INSERT INTO public.stepup_grants (user_id, session_id, credential_id, challenge_id, action, target, "
         "params_hash, expires_at) VALUES (%s, %s, %s, %s, 'creator.activate', 't', %s, now() + interval '1 minute')",
         (uid, sid, cred, challenge, "0" * 64)),
        ("UPDATE public.stepup_grants SET consumed_at = NULL", ()),
        ("INSERT INTO public.session_assurance (session_id, user_id, credential_id, challenge_id, aal, expires_at) "
         "VALUES (%s, %s, %s, %s, 2, now() + interval '1 hour')", (sid, uid, cred, challenge)),
        ("UPDATE public.stepup_challenges SET outcome = 'ok', used_at = now()", ()),
        ("UPDATE public.stepup_enforcement SET state = 'active', changed_by = 'attacker'", ()),
        ("INSERT INTO public.stepup_events (event, user_id) VALUES ('credential_approved', %s)", (uid,)),
        ("DELETE FROM public.stepup_events", ()),
        ("DELETE FROM public.stepup_rate_limits", ()),
        ("SET ROLE hyperchat_stepup", ()),
        ("SET ROLE hyperchat_owner", ()),
        ("SELECT public.stepup_approve_credential('a', 'b', 'c', 'd')", ()),
        ("SELECT public.stepup_recover_admin('a', 'b', 'c')", ()),
        ("SELECT public.stepup_consumed_here(%s, 'a', 'b', 'c')", (uid,)),
        ("SELECT public.stepup_admin_ok(%s)", (uid,)),
    ]
    for sql, args in attempts:
        with db(role) as conn, pytest.raises(REFUSED):
            conn.execute(sql, args)
    with db(role) as conn:  # the enforcement state is readable (S2 needs it) and still draft
        assert conn.execute("SELECT state FROM public.stepup_enforcement").fetchone()[0] == "draft"
    with db("hyperchat_readonly") as conn, pytest.raises(REFUSED):
        conn.execute("SELECT 1 FROM public.webauthn_credentials")  # migration 0007's default grant was revoked


def test_the_verifier_role_cannot_approve_promote_or_forge(verifier):
    uid, sid = admin("su-verifier-role")
    _, fingerprint = register(verifier, uid, sid)
    other_uid, other_sid = admin("su-verifier-other")
    with db("hyperchat_stepup") as conn:
        cred, challenge = conn.execute("SELECT c.id, ch.id FROM public.webauthn_credentials c "
                                       "JOIN public.stepup_challenges ch ON ch.user_id = c.user_id WHERE c.user_id = %s",
                                       (uid,)).fetchone()
    attempts = [
        ("UPDATE public.webauthn_credentials SET status = 'active', approved_at = now(), approved_by = 'me' "
         "WHERE id = %s", (cred,)),  # no UPDATE privilege on status
        ("UPDATE public.webauthn_credentials SET sign_count = 5 WHERE id = %s", (cred,)),  # pending: cannot be used
        ("INSERT INTO public.webauthn_credentials (user_id, credential_id, public_key, fingerprint, status) "
         "VALUES (%s, %s, %s, 'BBBB-BBBB-BBBB-BBBB-BBBB', 'active')",
         (uid, secrets.token_bytes(32), secrets.token_bytes(77))),  # no privilege on status: inserted pending only
        ("UPDATE public.stepup_enforcement SET state = 'active', changed_by = 'verifier'", ()),
        ("UPDATE public.stepup_events SET detail = '{}'", ()),
        ("DELETE FROM public.stepup_events", ()),
        ("TRUNCATE public.stepup_events", ()),
        ("INSERT INTO public.stepup_grants (user_id, session_id, credential_id, challenge_id, action, target, "
         "params_hash, expires_at) VALUES (%s, %s, %s, %s, 'mfa.register', 'self', %s, now() + interval '1 minute')",
         (uid, sid, cred, challenge, "0" * 64)),  # the registration challenge was not an action ceremony
        ("INSERT INTO public.stepup_challenges (user_id, session_id, purpose, action, target, params_hash, canonical, "
         "challenge, expires_at) VALUES (%s, %s, 'action', 'creator.activate', 't', %s, '{}', %s, "
         "now() + interval '1 minute')", (uid, sid, "0" * 64, secrets.token_bytes(32))),  # not the canonical's hash
        ("INSERT INTO public.stepup_challenges (user_id, session_id, purpose, action, target, params_hash, canonical, "
         "challenge, expires_at) SELECT %s, %s, purpose, action, target, params_hash, canonical, challenge, expires_at "
         "FROM public.stepup_challenges WHERE id = %s", (other_uid, other_sid, challenge)),  # another admin's session
        ("UPDATE public.stepup_challenges SET outcome = 'ok' WHERE id = %s", (challenge,)),  # already used
        ("SELECT public.stepup_approve_credential('su-verifier-role@authtest.invalid', %s, 'v', 'v')", (fingerprint,)),
        ("SET ROLE hyperchat_owner", ()),
    ]
    for sql, args in attempts:
        with db("hyperchat_stepup") as conn, pytest.raises((REFUSED, psycopg.errors.CheckViolation,
                                                            psycopg.errors.UniqueViolation)):
            conn.execute(sql, args)


def _verifier_sql(sql: str, args: tuple) -> None:
    with db("hyperchat_stepup") as conn, pytest.raises(REFUSED):  # exactly the guard, not a constraint
        conn.execute(sql, args)


def test_the_verifier_role_cannot_issue_a_grant_without_an_action_ceremony_verified_now(verifier):
    """Each forgery passes every other check (active passkey, same admin and session, unused challenge id)."""
    uid, sid, auth = enrolled(verifier, "su-forge-grant")
    session = run(verifier.assertion_options(uid, sid, "session.aal2", "session", {}))
    run(verifier.assertion_verify(uid, sid, session["challenge_id"], auth.assert_(session["public_key"])))
    failed = run(verifier.assertion_options(uid, sid, **ACTIVATE))
    refusal(verifier.assertion_verify(uid, sid, failed["challenge_id"],
                                      auth.assert_(failed["public_key"], origin="https://evil.example")))
    unused = run(verifier.assertion_options(uid, sid, **ACTIVATE))
    with db("hyperchat_stepup") as conn:
        cred = conn.execute("SELECT id FROM public.webauthn_credentials WHERE user_id = %s AND status = 'active'",
                            (uid,)).fetchone()[0]
        rows = {cid: conn.execute("SELECT action, target, params_hash FROM public.stepup_challenges WHERE id = %s",
                                  (cid,)).fetchone()
                for cid in (session["challenge_id"], failed["challenge_id"], unused["challenge_id"])}
    for cid, (action, what, params_hash) in rows.items():  # session ceremony / failed / never verified
        _verifier_sql("INSERT INTO public.stepup_grants (user_id, session_id, credential_id, challenge_id, action, "
                      "target, params_hash, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now() + interval '1 minute')",
                      (uid, sid, cred, cid, action, what, params_hash))
    ok = grant(verifier, uid, sid, auth)  # a verified action challenge, but from an earlier transaction
    with db("hyperchat_stepup") as conn:
        cid = conn.execute("SELECT challenge_id FROM public.stepup_grants WHERE id = %s", (ok["grant_id"],)).fetchone()[0]
    with fixture_db() as conn:  # free the challenge for a second grant (owner), so only "verified now" can refuse it
        conn.execute("DELETE FROM public.stepup_grants WHERE id = %s", (ok["grant_id"],))
    _verifier_sql("INSERT INTO public.stepup_grants (user_id, session_id, credential_id, challenge_id, action, target, "
                  "params_hash, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now() + interval '1 minute')",
                  (uid, sid, cred, cid, ok["action"], ok["target"], ok["params_hash"]))


def test_the_verifier_role_cannot_store_a_challenge_that_is_not_its_canonical_hash_or_not_the_admins_session(verifier):
    from app.stepup import canonical
    uid, sid, _ = enrolled(verifier, "su-forge-challenge")
    _, other_sid = admin("su-forge-challenge-other")
    insert = ("INSERT INTO public.stepup_challenges (user_id, session_id, purpose, action, target, params_hash, "
              "canonical, challenge, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, to_timestamp(%s))")
    good = canonical.build(rp_id=RP, admin_id=uid, session_id=sid, **ACTIVATE)
    with db("hyperchat_stepup") as conn:  # the genuine row is accepted ...
        conn.execute(insert, (uid, sid, good.purpose, good.action, good.target, good.params_hash, good.canonical,
                              good.challenge, good.expires_at))
    again = canonical.build(rp_id=RP, admin_id=uid, session_id=sid, **ACTIVATE)  # ... a random challenge is not
    _verifier_sql(insert, (uid, sid, again.purpose, again.action, again.target, again.params_hash, again.canonical,
                           secrets.token_bytes(32), again.expires_at))
    theirs = canonical.build(rp_id=RP, admin_id=uid, session_id=other_sid, **ACTIVATE)  # canonical consistent, but the
    _verifier_sql(insert, (uid, other_sid, theirs.purpose, theirs.action, theirs.target, theirs.params_hash,  # session
                           theirs.canonical, theirs.challenge, theirs.expires_at))  # belongs to another administrator


def test_the_audit_history_is_append_only_even_for_the_owner(verifier):
    enrolled(verifier, "su-audit")
    with fixture_db() as conn:
        for sql in ("UPDATE public.stepup_events SET operator = 'x'", "DELETE FROM public.stepup_events",
                    "TRUNCATE public.stepup_events", "DELETE FROM public.stepup_enforcement"):
            with pytest.raises(REFUSED):
                conn.execute(sql)
        events = {r[0] for r in conn.execute(
            "SELECT event FROM public.stepup_events e JOIN public.users u ON u.id = e.user_id WHERE u.email = %s",
            ("su-audit" + DOMAIN,))}
    assert {"challenge_issued", "registration_ok", "credential_approved"} <= events


# ── operator ────────────────────────────────────────────────────────────────────

def test_approval_needs_the_administrators_own_pending_fingerprint(verifier):
    uid, sid = admin("su-approve")
    _, fingerprint = register(verifier, uid, sid)
    other_uid, other_sid = admin("su-approve-other")
    _, other_fp = register(verifier, other_uid, other_sid)
    with fixture_db() as conn:
        call = "SELECT public.stepup_approve_credential(%s, %s, %s, %s)"
        assert conn.execute(call, ("su-approve" + DOMAIN, other_fp, "op", "call")).fetchone()[0] == "not_found"
        assert conn.execute(call, ("su-approve" + DOMAIN, fingerprint, "op", " ")).fetchone()[0] \
            == "operator_and_verification_required"
        assert conn.execute(call, ("su-approve" + DOMAIN, fingerprint.lower(), "op", "call")).fetchone()[0] == "ok"
        assert conn.execute(call, ("su-approve" + DOMAIN, fingerprint, "op", "call")).fetchone()[0] == "not_pending"
    with guards_off("webauthn_credentials") as conn:
        conn.execute("UPDATE public.webauthn_credentials SET created_at = now() - interval '4 days' "
                     "WHERE fingerprint = %s", (other_fp,))
    with fixture_db() as conn:
        assert conn.execute("SELECT public.stepup_approve_credential(%s, %s, 'op', 'call')",
                            ("su-approve-other" + DOMAIN, other_fp)).fetchone()[0] == "stale"


def test_recovery_revokes_every_passkey_and_session(verifier):
    uid, sid, auth = enrolled(verifier, "su-recover")
    register(verifier, uid, sid)  # a second, still pending
    with fixture_db() as conn:
        assert conn.execute("SELECT public.stepup_recover_admin(%s, 'op', 'both phones lost')",
                            ("su-recover" + DOMAIN,)).fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM public.sessions WHERE user_id = %s AND revoked_at IS NULL",
                            (uid,)).fetchone()[0] == 0
        assert {r[0] for r in conn.execute("SELECT status FROM public.webauthn_credentials WHERE user_id = %s",
                                           (uid,))} == {"revoked"}


def test_enforcement_needs_two_active_passkeys_for_every_active_administrator(verifier):
    with db() as conn:
        others = conn.execute("SELECT count(*) FROM public.users WHERE role = 'admin' AND is_active "
                              "AND email::text NOT LIKE %s", ("%" + DOMAIN,)).fetchone()[0]
    if others:
        pytest.skip("the scratch database has real administrators")
    with fixture_db() as conn:  # this suite's other synthetic admins must not count
        conn.execute("UPDATE public.users SET role = 'streamer' WHERE role = 'admin' AND email::text LIKE %s",
                     ("%" + DOMAIN,))
    uid, sid, _ = enrolled(verifier, "su-ready")
    activate = "UPDATE public.stepup_enforcement SET state = 'active', changed_by = 'test-operator'"
    with fixture_db() as conn, pytest.raises(REFUSED):
        conn.execute(activate)
    _, fingerprint = register(verifier, uid, sid)
    assert approve("su-ready" + DOMAIN, fingerprint) == "ok"
    with fixture_db() as conn:
        try:
            conn.execute(activate)
            assert conn.execute("SELECT state FROM public.stepup_enforcement").fetchone()[0] == "active"
        finally:
            conn.execute("UPDATE public.stepup_enforcement SET state = 'draft', changed_by = 'test-operator'")


def test_the_operator_cli_approves_revokes_and_reports(verifier, capsys, monkeypatch):
    from app.stepup.operator import main
    name, port = target()
    monkeypatch.setenv("HYPERCHAT_MIGRATION_DSN", f"host={PG_HOST} port={port} dbname={name} user={FIXTURE_ROLE}")
    uid, sid = admin("su-cli")
    _, fingerprint = register(verifier, uid, sid)
    email = "su-cli" + DOMAIN
    common = ["--email", email, "--operator", "test-operator"]
    assert main(["approve", *common, "--fingerprint", fingerprint, "--verified-by", "video call",
                 "--confirm-database", "not-this-one"]) == 2
    assert main(["approve", *common, "--fingerprint", fingerprint, "--verified-by", "video call",
                 "--confirm-database", name]) == 0
    assert main(["list", "--email", email]) == 0 and "active" in capsys.readouterr().out
    assert main(["readiness"]) == 0 and '"enforcement": "draft"' in capsys.readouterr().out
    assert main(["revoke", *common, "--fingerprint", fingerprint, "--reason", "test", "--confirm-database", name]) == 0
    assert main(["revoke", *common, "--fingerprint", fingerprint, "--reason", "test", "--confirm-database", name]) == 2
    assert main(["recover", *common, "--reason", "test", "--confirm-database", name]) == 0
