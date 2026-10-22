"""KYC PR3 (migration 0022) through the real app on the scratch database: owner session API with a fake Didit (no
real call), signed webhooks verified by the database (signature, freshness, replay, identity, ordering,
concurrency), the approval rule, ownership, admin views / refresh (never approves; ordered by when Didit was asked),
the trust boundary of the API's database role (it can never forge an approval), and that nothing else changes (no
activation, no eligibility change). Synthetic rows only."""

import asyncio
import hashlib
import hmac
import json
import threading
import time
import uuid

import psycopg
import pytest
from pydantic import SecretStr

from app.kyc import didit
from app.kyc.didit import CreatedSession, DiditError
from app.kyc.service import KycRepository, KycService
from tests.integration import (
    DOMAIN,
    ORIGIN,
    db,
    fixture_db,
    make_donation,
    make_streamer,
    make_user,
    new_browser,
    requires_db,
    signed_in,
)

pytestmark = requires_db
SECRET = "whsec_kyc_test_secret"
WF = "6ff16570-fe12-4212-9647-b12b8898cc09"
APP_ID = "0f0f0f0f-1111-4222-8333-444444444444"
ME = "/api/v1/me/kyc"
HOOK = "/api/v1/webhooks/didit"


class FakeDidit:
    """Didit's documented behaviour: one unfinished session per vendor_data (returned again), a new one after a
    finished session; decisions as scripted. Never a network call."""

    def __init__(self, workflow: str = WF) -> None:
        self.workflow_id = workflow
        self.sessions: dict[str, str] = {}   # vendor -> open session id
        self.decisions: dict[str, dict] = {}
        self.fail: str | None = None
        self.calls = 0

    async def create_session(self, vendor_data, callback):
        self.calls += 1
        if self.fail:
            raise DiditError(self.fail)
        assert callback.endswith("/kyc/return")
        sid = self.sessions.setdefault(vendor_data, str(uuid.uuid4()))
        return CreatedSession(sid, f"https://verify.didit.me/session/{sid[:8]}", "Not Started", 1)

    def finish(self, vendor_data):
        self.sessions.pop(vendor_data, None)

    async def decision(self, session_id):
        if self.fail:
            raise DiditError(self.fail)
        return self.decisions[session_id]


def configure(environment="sandbox", workflow=WF) -> None:
    """What the operator does with etl/kyc_provider_config.py: the database's Didit application and webhook secret."""
    with fixture_db() as conn:
        conn.execute("INSERT INTO public.kyc_provider_config (environment, workflow_id, application_id, webhook_secret) "
                     "VALUES (%s, %s, %s, %s) ON CONFLICT (singleton) DO UPDATE SET environment = EXCLUDED.environment, "
                     "workflow_id = EXCLUDED.workflow_id, application_id = EXCLUDED.application_id, "
                     "webhook_secret = EXCLUDED.webhook_secret", (environment, workflow, APP_ID, SECRET))


def unconfigure() -> None:
    with fixture_db() as conn:
        conn.execute("DELETE FROM public.kyc_provider_config")


@pytest.fixture
def kyc(app):
    """The app's KYC service, configured for a sandbox application with a fake Didit; the database holds the
    webhook secret (the API's settings have none)."""
    settings = app.app_state.settings.model_copy(update={
        "didit_api_key": SecretStr("test-key"), "didit_workflow_id": WF, "didit_environment": "sandbox"})
    fake = FakeDidit()
    app.app_state.kyc = KycService(app.app_state.kyc.repo, settings, client=fake, limiter=app.app_state.auth.repo)
    app.fake = fake
    configure()
    yield app
    unconfigure()


def sign(body: dict, secret: str = SECRET) -> str:
    """Didit's X-Signature-V2, computed independently of the server (which verifies it in the database)."""
    return hmac.new(secret.encode(), didit.canonical(body), hashlib.sha256).hexdigest()


def owner(client, name: str, slug: str | None = None) -> tuple[dict, str]:
    """A signed-in user who owns a (legacy, existing-style) creator: (CSRF headers, user id)."""
    headers = signed_in(client, name + DOMAIN)
    with db() as conn:
        uid = conn.execute("SELECT id::text FROM public.users WHERE email = %s", (name + DOMAIN,)).fetchone()[0]
    make_streamer(slug or f"authtest-{name}", user_id=uid)
    return headers, uid


def envelope(*, session_id, vendor, status, created_at, event_id=None, decision=None, ts=None, **extra) -> dict:
    body = {"event_id": event_id or f"authtest-ev-{uuid.uuid4().hex[:12]}", "webhook_type": "status.updated",
            "timestamp": int(time.time()) if ts is None else ts, "created_at": created_at, "application_id": APP_ID,
            "environment": "sandbox", "session_id": session_id, "status": status, "workflow_id": WF,
            "workflow_version": 1, "vendor_data": vendor,
            "metadata": {"note": "Ravi शर्मा 🎉 é", "score": 99.0, "ratio": 0.5}, **extra}  # canonical-form cases
    if decision is not None:
        body["decision"] = decision
    return body


def hook(app, *, secret=SECRET, x_ts=None, sign_it=True, signature=None, **kw):
    body = envelope(**kw)
    headers = {"content-type": "application/json", "x-timestamp": str(int(time.time()) if x_ts is None else x_ts)}
    if sign_it:
        headers["x-signature-v2"] = signature or sign(body, secret)
    return app.post(HOOK, content=json.dumps(body), headers=headers)


def deliver(conn, **kw) -> str:
    """A signed delivery straight to the database function, on the caller's connection (and transaction)."""
    body = envelope(**kw)
    return conn.execute("SELECT public.kyc_receive_webhook(%s, %s, %s)",
                        (didit.canonical(body).decode(), sign(body), str(body["timestamp"]))).fetchone()[0]


def wait_for_a_lock_waiter() -> None:
    with db() as conn:
        for _ in range(200):
            if conn.execute("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND NOT granted)"
                            ).fetchone()[0]:
                return
            time.sleep(0.05)
    raise AssertionError("the second writer never waited for the first one")


def approved_decision(sid: str) -> dict:
    return {"session_id": sid, "status": "Approved",
            "id_verifications": [{"status": "Approved", "first_name": "Asha", "document_number": "DOC-SECRET-77",
                                  "date_of_birth": "1990-02-03", "address": "12 Secret Lane",
                                  "portrait_image": "https://media.didit.me/portrait?signature=abc"}],
            "liveness_checks": [{"status": "Approved", "video_url": "https://media.didit.me/v?signature=abc"}],
            "face_matches": [{"status": "Approved"}], "aml_screenings": [{"status": "Approved"}]}


def session_row(sid: str) -> tuple:
    with db() as conn:
        return conn.execute("SELECT provider_status, user_id::text, vendor_data FROM public.kyc_sessions "
                            "WHERE session_id = %s", (sid,)).fetchone()


def approved(user_id: str) -> bool:
    with db() as conn:
        return conn.execute("SELECT public.user_kyc_approved(%s, 'sandbox')", (user_id,)).fetchone()[0]


def start(app, headers) -> dict:
    r = app.post(f"{ME}/session", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ── owner session API ───────────────────────────────────────────────────────────

def test_the_owner_starts_and_resumes_one_session(kyc):
    headers, uid = owner(kyc, "kyc-own")
    assert kyc.get(ME).json() == {"approved": False, "state": "none", "provider_status": None, "updated_at": None,
                                  "owns_creator": True, "available": True, "can_start": True}
    first = start(kyc, headers)
    assert set(first) == {"session_id", "status", "url"} and first["url"].startswith("https://verify.didit.me/")
    assert start(kyc, headers)["session_id"] == first["session_id"]  # resumed, not duplicated
    assert session_row(first["session_id"]) == ("Not Started", uid, uid)  # vendor_data = the immutable user id
    assert kyc.get(ME).json()["state"] == "in_progress"
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.kyc_sessions WHERE user_id = %s", (uid,)).fetchone()[0] == 1
        assert conn.execute("SELECT action FROM public.kyc_events WHERE user_id = %s", (uid,)).fetchall() == [
            ("session_created",)]


def test_only_a_creator_owner_may_start(kyc):
    assert kyc.post(f"{ME}/session", headers=ORIGIN).status_code == 401
    plain = new_browser(kyc)
    h = signed_in(plain, "kyc-plain" + DOMAIN)
    r = plain.post(f"{ME}/session", headers=h)
    assert (r.status_code, r.json()["error"]) == (403, "Only a creator's owner can verify their identity here")
    sid = make_streamer("authtest-kyc-linked", user_id=make_user("kyc-realowner" + DOMAIN))
    linked = new_browser(kyc)
    h2 = signed_in(linked, "kyc-linked" + DOMAIN, streamer_id=sid)  # a linked account (moderator), not the owner
    assert linked.post(f"{ME}/session", headers=h2).status_code == 403
    assert plain.get(ME).json()["can_start"] is False
    headers, _ = owner(kyc, "kyc-csrf")
    assert kyc.post(f"{ME}/session", headers=ORIGIN).status_code == 403  # no CSRF token
    assert kyc.post(f"{ME}/session", headers={**headers, "origin": "https://evil.example"}).status_code == 403
    assert kyc.fake.calls == 0


def test_rate_limited_per_user(kyc):
    headers, _ = owner(kyc, "kyc-rate")
    codes = [kyc.post(f"{ME}/session", headers=headers).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]


def test_unconfigured_kyc_is_unavailable_and_nothing_else_breaks(app):
    unconfigure()
    headers, _ = owner(app, "kyc-off")
    r = app.post(f"{ME}/session", headers=headers)
    assert (r.status_code, r.json()["error"]) == (503, "Identity verification is unavailable right now. Please try "
                                                       "again later.")
    assert app.get(ME).json()["available"] is False
    assert app.get("/api/v1/public/streamers/authtest-kyc-off").status_code == 200
    assert app.post(HOOK, content=b"{}", headers={"content-type": "application/json"}).status_code == 503


def test_provider_failure_and_a_lost_local_write_recover_without_duplicates(kyc):
    headers, uid = owner(kyc, "kyc-fail")
    kyc.fake.fail = "timeout"
    r = kyc.post(f"{ME}/session", headers=headers)
    assert (r.status_code, r.json()["error"]) == (503, "Identity verification is unavailable right now. Please try "
                                                       "again later.")
    assert "test-key" not in r.text
    kyc.fake.fail = None
    repo = kyc.app_state.kyc.repo
    real = repo.record_session

    async def lost(*args, **kwargs):
        raise DiditError("database_unavailable")  # Didit created the session; our write did not happen

    repo.record_session = lost
    assert kyc.post(f"{ME}/session", headers=headers).status_code == 503
    repo.record_session = real
    sid = start(kyc, headers)["session_id"]  # Didit returns the same open session: stored now, once
    assert sid == kyc.fake.sessions[uid]
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.kyc_sessions WHERE user_id = %s", (uid,)).fetchone()[0] == 1


def test_concurrent_starts_store_one_session(kyc, env):
    _, uid = owner(kyc, "kyc-conc")
    repo = KycRepository(kyc.app_state.kyc.repo._conn)

    async def both():
        created = CreatedSession(str(uuid.uuid4()), "https://verify.didit.me/s/x", "Not Started", 1)
        from app.cli import connection_factory
        from app.core.config import Settings
        r = KycRepository(connection_factory(Settings()))
        return await asyncio.gather(*(r.record_session(uid, created, WF, "sandbox") for _ in range(4)))

    rows = asyncio.run(both())
    assert len({row["session_id"] for row in rows}) == 1
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.kyc_sessions WHERE user_id = %s", (uid,)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM public.kyc_events WHERE user_id = %s", (uid,)).fetchone()[0] == 1
    del repo


# ── webhooks: authentication ────────────────────────────────────────────────────

def test_signature_freshness_and_application_are_enforced(kyc):
    headers, uid = owner(kyc, "kyc-sig")
    sid = start(kyc, headers)["session_id"]
    now = int(time.time())
    common = {"session_id": sid, "vendor": uid, "status": "Declined", "created_at": now}
    for kw, code in (({"sign_it": False}, 401), ({"secret": "wrong-secret-0123456789"}, 401),
                     ({"signature": "0" * 64}, 401), ({"signature": "not hex"}, 401), ({"ts": now - 301}, 401),
                     ({"x_ts": now - 301}, 401), ({"x_ts": "soon"}, 401), ({"ts": "1774970000"}, 401),
                     ({"ts": now + 400}, 401), ({"environment": "live"}, 403),
                     ({"application_id": "00000000-0000-4000-8000-000000000000"}, 403)):
        assert hook(kyc, **common, **kw).status_code == code, kw
    tampered = {"event_id": "authtest-ev-t", "webhook_type": "status.updated", "timestamp": now, "created_at": now,
                "application_id": APP_ID, "environment": "sandbox", "session_id": sid, "status": "Declined",
                "workflow_id": WF, "vendor_data": uid}
    signature = sign(tampered)
    tampered["status"] = "Approved"
    r = kyc.post(HOOK, content=json.dumps(tampered), headers={"content-type": "application/json",
                                                               "x-timestamp": str(now), "x-signature-v2": signature})
    assert r.status_code == 401
    assert kyc.post(HOOK, content=b"[]", headers={"content-type": "text/plain"}).status_code == 415
    assert kyc.post(HOOK, content=b"x" * (513 * 1024), headers={"content-type": "application/json"}).status_code == 413
    assert session_row(sid)[0] == "Not Started"  # nothing applied
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.kyc_webhook_events WHERE session_id = %s",
                            (sid,)).fetchone()[0] == 0  # unauthenticated deliveries are not even recorded
    good = envelope(**common)
    r = kyc.post(HOOK, content=json.dumps(good, indent=1), headers={  # other spacing, upper-case hex: still Didit's
        "content-type": "application/json", "x-timestamp": str(now), "x-signature-v2": sign(good).upper()})
    assert r.json() == {"status": "applied"} and session_row(sid)[0] == "Declined"


@pytest.mark.parametrize("key", [b"k", b"whsec_" + b"x" * 58, b"\xaa" * 131])
def test_the_database_hmac_is_rfc2104_hmac_sha256(key):
    message = '{"name":"Ravi शर्मा 🎉","score":99}'.encode()
    with fixture_db() as conn:
        assert conn.execute("SELECT encode(public.kyc_hmac_sha256(%s, %s), 'hex')", (key, message)).fetchone()[0] == \
            hmac.new(key, message, hashlib.sha256).hexdigest()


def test_duplicates_and_replays_are_applied_once(kyc):
    headers, uid = owner(kyc, "kyc-dup")
    sid = start(kyc, headers)["session_id"]
    t = int(time.time())
    first = hook(kyc, session_id=sid, vendor=uid, status="In Progress", created_at=t, event_id="authtest-ev-dup")
    again = hook(kyc, session_id=sid, vendor=uid, status="In Progress", created_at=t, event_id="authtest-ev-dup")
    assert first.json() == {"status": "applied"} and again.json() == {"status": "duplicate"}
    replay = hook(kyc, session_id=sid, vendor=uid, status="In Progress", created_at=t, event_id="authtest-ev-dup",
                  ts=t - 400, x_ts=t - 400)
    assert replay.status_code == 401  # an old capture is refused before it reaches the dedupe
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.kyc_events WHERE user_id = %s AND action = 'status_changed'",
                            (uid,)).fetchone()[0] == 1


# ── webhooks: identity and completeness ─────────────────────────────────────────

def test_an_event_for_another_user_workflow_or_without_a_decision_never_approves(kyc):
    headers, uid = owner(kyc, "kyc-id")
    sid = start(kyc, headers)["session_id"]
    other = make_user("kyc-other" + DOMAIN)
    t = int(time.time())
    cases = [
        ({"vendor": other, "decision": approved_decision(sid)}, "identity_mismatch"),
        ({"vendor": uid, "decision": approved_decision(sid), "workflow_id": str(uuid.uuid4())}, "identity_mismatch"),
        ({"vendor": uid}, "incomplete"),                                           # Approved without its decision
        ({"vendor": uid, "decision": approved_decision(str(uuid.uuid4()))}, "incomplete"),  # decision for another
        ({"vendor": uid, "decision": {**approved_decision(sid), "status": "Declined"}}, "incomplete"),
        ({"vendor": "not-a-uuid", "decision": approved_decision(sid)}, "identity_mismatch"),
    ]
    for kw, outcome in cases:
        r = hook(kyc, session_id=sid, status="Approved", created_at=t, **kw)
        assert r.json() == {"status": outcome}, kw
    assert hook(kyc, session_id=sid, vendor=uid, status="Totally New", created_at=t).json() == {
        "status": "unknown_status"}
    assert session_row(sid)[0] == "Not Started" and not approved(uid) and not approved(other)


def test_approved_from_a_complete_signed_event_and_minimal_storage(kyc):
    headers, uid = owner(kyc, "kyc-ok", slug="authtest-kyc-ok")
    sid = start(kyc, headers)["session_id"]
    with db() as conn:
        before = conn.execute("SELECT status, user_id, public.creator_donation_block(id) FROM public.streamers "
                              "WHERE streamer_slug = 'authtest-kyc-ok'").fetchone()
    r = hook(kyc, session_id=sid, vendor=uid, status="Approved", created_at=int(time.time()),
             decision=approved_decision(sid))
    assert r.json() == {"status": "applied"} and approved(uid)
    me = kyc.get(ME).json()
    assert me["approved"] is True and me["state"] == "approved" and me["can_start"] is False
    assert kyc.post(f"{ME}/session", headers=headers).status_code == 409  # already verified
    with db() as conn:
        after = conn.execute("SELECT status, user_id, public.creator_donation_block(id) FROM public.streamers "
                             "WHERE streamer_slug = 'authtest-kyc-ok'").fetchone()
        stored = "\n".join(str(r) for t in ("kyc_sessions", "kyc_webhook_events", "kyc_events")
                           for r in conn.execute(f"SELECT * FROM public.{t}"))
        checks = conn.execute("SELECT check_statuses FROM public.kyc_sessions WHERE session_id = %s",
                              (sid,)).fetchone()[0]
    assert after == before  # no activation, no eligibility change (PR4 wires KYC in)
    assert checks == {"id_verifications": ["Approved"], "liveness_checks": ["Approved"], "face_matches": ["Approved"],
                      "aml_screenings": ["Approved"]}
    for secret in ("DOC-SECRET-77", "Asha", "1990-02-03", "Secret Lane", "media.didit.me", "signature=abc"):
        assert secret not in stored, secret
        assert not any(secret in rec for rec in kyc.app_state.log_capture.records), secret


# ── webhooks: ordering, revocation, resubmission, concurrency ───────────────────

def test_order_is_the_providers_and_later_revocation_or_expiry_wins(kyc):
    headers, uid = owner(kyc, "kyc-order")
    sid = start(kyc, headers)["session_id"]
    t = int(time.time())
    assert hook(kyc, session_id=sid, vendor=uid, status="Approved", created_at=t,
                decision=approved_decision(sid)).json() == {"status": "applied"}
    late_old = hook(kyc, session_id=sid, vendor=uid, status="In Progress", created_at=t - 60)
    assert late_old.json() == {"status": "stale"} and approved(uid)  # a delayed older event never downgrades
    assert hook(kyc, session_id=sid, vendor=uid, status="Kyc Expired", created_at=t + 5).json() == {"status": "applied"}
    assert not approved(uid) and kyc.get(ME).json()["state"] == "expired"
    kyc.fake.finish(uid)  # a finished session: Didit hands out a new one
    sid2 = start(kyc, headers)["session_id"]
    assert sid2 != sid
    assert hook(kyc, session_id=sid2, vendor=uid, status="Declined", created_at=t + 10).json()["status"] == "applied"
    assert hook(kyc, session_id=sid2, vendor=uid, status="Resubmitted", created_at=t + 20).json()["status"] == "applied"
    assert not approved(uid) and kyc.get(ME).json()["state"] == "in_progress"
    assert hook(kyc, session_id=sid2, vendor=uid, status="Approved", created_at=t + 30,
                decision=approved_decision(sid2)).json()["status"] == "applied"
    assert approved(uid)
    assert hook(kyc, session_id=sid2, vendor=uid, status="Declined", created_at=t + 40).json()["status"] == "applied"
    assert not approved(uid)  # a later valid revocation removes the approval


def test_an_early_webhook_is_applied_when_the_session_is_stored(kyc):
    headers, uid = owner(kyc, "kyc-early")
    future = str(uuid.uuid4())
    kyc.fake.sessions[uid] = future  # Didit already created it; our row does not exist yet
    t = int(time.time())
    assert hook(kyc, session_id=future, vendor=uid, status="In Progress", created_at=t).json() == {
        "status": "unmatched"}
    other = make_user("kyc-early-other" + DOMAIN)
    hook(kyc, session_id=future, vendor=other, status="Declined", created_at=t + 1)  # signed for someone else
    assert start(kyc, headers)["session_id"] == future
    assert session_row(future)[0] == "In Progress"  # applied; the other user's event was not
    with db() as conn:
        assert sorted(r[0] for r in conn.execute("SELECT outcome FROM public.kyc_webhook_events WHERE session_id = %s",
                                                 (future,))) == ["identity_mismatch", "matched_later"]  # none left


def test_concurrent_deliveries_keep_the_newest_provider_state(kyc):
    headers, uid = owner(kyc, "kyc-race")
    sid = start(kyc, headers)["session_id"]
    t = int(time.time())
    events = [("In Progress", t + 1, None), ("In Review", t + 2, None), ("Declined", t + 3, None),
              ("Approved", t + 4, approved_decision(sid)), ("In Progress", t + 0, None)]
    threads = [threading.Thread(target=hook, args=(kyc,), kwargs=dict(session_id=sid, vendor=uid, status=s,
                                                                     created_at=c, decision=d))
               for s, c, d in events]
    for th in threads:
        th.start()
    for th in threads:
        th.join(20)
    assert session_row(sid)[0] == "Approved" and approved(uid)


# ── the effective approval across sessions (one rule, SQL = API) ─────────────

def new_session(uid: str, workflow: str = WF) -> str:
    """Another Didit session of the user (as kyc_create_session stores it)."""
    sid = str(uuid.uuid4())
    with db() as conn:
        assert conn.execute("SELECT public.kyc_create_session(%s, %s, %s, 1, 'sandbox')",
                            (uid, sid, workflow)).fetchone()[0] == "ok"
    return sid


def me_approved(app, uid: str) -> bool:
    """The API's answer, which must always equal public.user_kyc_approved."""
    me = app.get(ME).json()
    assert me["approved"] is approved(uid)
    return me["approved"]


def test_the_newest_session_decides_and_a_later_adverse_result_wins(kyc):
    headers, uid = owner(kyc, "kyc-multi")
    t = int(time.time())

    def approve(sid, at):
        return hook(kyc, session_id=sid, vendor=uid, status="Approved", created_at=at,
                    decision=approved_decision(sid)).json()["status"]

    a = start(kyc, headers)["session_id"]
    assert approve(a, t) == "applied" and me_approved(kyc, uid)  # an ordinary single-session approval
    b = new_session(uid)  # a newer attempt: the older approval does not outlive it
    assert not me_approved(kyc, uid) and kyc.get(ME).json()["state"] == "in_progress"
    assert hook(kyc, session_id=b, vendor=uid, status="Declined", created_at=t + 10).json()["status"] == "applied"
    me = kyc.get(ME).json()
    assert (me["approved"], me["state"], me["can_start"]) == (False, "declined", True) and not approved(uid)
    c = new_session(uid)  # a later legitimate re-approval counts (the older decline does not block it)
    assert approve(c, t + 20) == "applied" and me_approved(kyc, uid)
    assert hook(kyc, session_id=a, vendor=uid, status="Kyc Expired", created_at=t + 30).json()["status"] == "applied"
    me = kyc.get(ME).json()  # an older session's later expiry / revocation also wins (fail closed)
    assert (me["approved"], me["state"], me["can_start"]) == (False, "reverification_required", True)
    assert not approved(uid)
    d = new_session(uid)
    assert approve(d, t + 40) == "applied" and me_approved(kyc, uid)
    with db() as conn:
        assert conn.execute("SELECT public.user_kyc_approved(%s, 'live')", (uid,)).fetchone()[0] is False
    assert hook(kyc, session_id=d, vendor=uid, status="Declined", created_at=t + 50).json()["status"] == "applied"
    assert not me_approved(kyc, uid) and kyc.get(ME).json()["state"] == "declined"  # same-session revocation


def test_a_workflow_change_requires_reverification(kyc):
    headers, uid = owner(kyc, "kyc-wf")
    t = int(time.time())
    a = start(kyc, headers)["session_id"]
    assert hook(kyc, session_id=a, vendor=uid, status="Approved", created_at=t,
                decision=approved_decision(a)).json()["status"] == "applied" and me_approved(kyc, uid)
    other = str(uuid.uuid4())
    configure(workflow=other)  # the operator switches this database to another Didit workflow
    me = kyc.get(ME).json()
    assert (me["approved"], me["state"], me["can_start"]) == (False, "reverification_required", True)
    assert not approved(uid)
    assert hook(kyc, session_id=a, vendor=uid, status="Approved", created_at=t + 5,  # the old workflow's events
                decision=approved_decision(a)).json()["status"] == "identity_mismatch"
    b = new_session(uid, other)
    assert hook(kyc, session_id=b, vendor=uid, status="Approved", created_at=t + 10, decision=approved_decision(b),
                workflow_id=other).json()["status"] == "applied"
    assert me_approved(kyc, uid)
    configure(workflow=WF)  # back to the first workflow: its old approval is not restored (a new epoch)
    me = kyc.get(ME).json()
    assert (me["approved"], me["state"], me["can_start"]) == (False, "reverification_required", True)
    assert not approved(uid)


def send(app, uid, sid, status, at, **kw) -> str:
    return hook(app, session_id=sid, vendor=uid, status=status, created_at=at, **kw).json()["status"]


def test_a_historical_revocation_is_a_durable_veto(kyc):
    headers, uid = owner(kyc, "kyc-veto")
    boss = new_browser(kyc)
    bh = signed_in(boss, "kyc-boss3" + DOMAIN, role="admin")
    t = int(time.time()) - 1000  # provider times in the past, so an admin refresh (dated now) is the newest
    a = start(kyc, headers)["session_id"]
    c = new_session(uid)

    def approve(at):
        return send(kyc, uid, c, "Approved", at, decision=approved_decision(c))

    assert approve(t + 20) == "applied" and me_approved(kyc, uid)
    assert send(kyc, uid, a, "Declined", t + 30) == "applied" and not me_approved(kyc, uid)
    assert send(kyc, uid, a, "Resubmitted", t + 40) == "applied" and not me_approved(kyc, uid)  # veto survives
    assert send(kyc, uid, a, "In Progress", t + 50) == "applied" and not me_approved(kyc, uid)
    assert approve(t + 60) == "applied" and me_approved(kyc, uid)  # only a later signed approval restores it
    assert send(kyc, uid, a, "Kyc Expired", t + 70) == "applied" and not me_approved(kyc, uid)
    assert send(kyc, uid, a, "Resubmitted", t + 80) == "applied" and not me_approved(kyc, uid)
    assert approve(t + 90) == "applied" and me_approved(kyc, uid)
    assert send(kyc, uid, a, "Declined", t + 90) == "applied" and not me_approved(kyc, uid)  # equal time: veto wins
    assert approve(t + 100) == "applied" and me_approved(kyc, uid)
    # An admin refresh that records an adverse state is a veto too; a later non-approving refresh does not erase it.
    kyc.fake.decisions[c] = {"session_id": c, "status": "Declined"}
    assert boss.post(f"/api/v1/admin/users/{uid}/kyc/refresh", headers=bh).json()["refresh"] == "applied"
    assert not me_approved(kyc, uid)
    kyc.fake.decisions[c] = {"session_id": c, "status": "In Review"}
    assert boss.post(f"/api/v1/admin/users/{uid}/kyc/refresh", headers=bh).json()["refresh"] == "applied"
    assert not me_approved(kyc, uid) and session_row(c)[0] == "In Review"
    # The veto is not a webhook receipt: the 90-day purge does not lift it.
    with fixture_db() as conn:
        conn.execute("UPDATE public.kyc_webhook_events SET received_at = now() - interval '91 days' "
                     "WHERE session_id IN (%s, %s)", (a, c))
    with db() as conn:
        assert conn.execute("SELECT public.kyc_purge_webhook_receipts(false)").fetchone()[0] >= 10
    assert not me_approved(kyc, uid)
    assert approve(int(time.time()) + 5) == "applied" and me_approved(kyc, uid)


def test_a_stale_revocation_still_vetoes(kyc):
    headers, uid = owner(kyc, "kyc-veto-stale")
    t = int(time.time()) - 1000
    a = start(kyc, headers)["session_id"]
    c = new_session(uid)
    assert send(kyc, uid, a, "In Progress", t + 50) == "applied"
    assert send(kyc, uid, c, "Approved", t + 20, decision=approved_decision(c)) == "applied" and me_approved(kyc, uid)
    assert send(kyc, uid, a, "Declined", t + 30) == "stale"  # older than A's own state, newer than C's approval
    assert not me_approved(kyc, uid) and session_row(a)[0] == "In Progress"


def test_concurrent_adverse_and_approval_events_keep_the_veto(kyc):
    headers, uid = owner(kyc, "kyc-veto-race")
    t = int(time.time()) - 1000
    a = start(kyc, headers)["session_id"]
    c = new_session(uid)
    events = [(a, "Declined", t + 80, None), (a, "Resubmitted", t + 90, None), (a, "In Progress", t + 85, None),
              (c, "Approved", t + 75, approved_decision(c))]
    threads = [threading.Thread(target=send, args=(kyc, uid, sid, status, at), kwargs={"decision": d})
               for sid, status, at, d in events]
    for th in threads:
        th.start()
    for th in threads:
        th.join(20)
    assert session_row(a)[0] == "Resubmitted" and session_row(c)[0] == "Approved"
    assert not me_approved(kyc, uid)  # whatever the arrival order
    with db() as conn:
        assert conn.execute("SELECT extract(epoch FROM adverse_at)::bigint FROM public.kyc_sessions "
                            "WHERE session_id = %s", (a,)).fetchone()[0] == t + 80


def use_workflow(app, workflow: str) -> None:
    """The operator moves this environment to another Didit workflow: database configuration and API settings."""
    configure(workflow=workflow)
    settings = app.app_state.kyc.settings.model_copy(update={"didit_workflow_id": workflow})
    app.fake = FakeDidit(workflow)
    app.app_state.kyc = KycService(app.app_state.kyc.repo, settings, client=app.fake, limiter=app.app_state.auth.repo)


def test_an_obsolete_workflow_session_never_blocks_reverification(kyc):
    headers, uid = owner(kyc, "kyc-wf-review")
    a = start(kyc, headers)["session_id"]
    assert send(kyc, uid, a, "In Review", int(time.time())) == "applied"
    me = kyc.get(ME).json()
    assert (me["state"], me["can_start"]) == ("in_review", False)
    assert kyc.post(f"{ME}/session", headers=headers).status_code == 409
    other = str(uuid.uuid4())
    use_workflow(kyc, other)
    me = kyc.get(ME).json()
    assert (me["approved"], me["state"], me["can_start"]) == (False, "reverification_required", True)
    b = start(kyc, headers)["session_id"]  # the real owner path, under the new workflow
    assert b != a and kyc.get(ME).json()["state"] == "in_progress"
    with db() as conn:
        assert conn.execute("SELECT workflow_id::text FROM public.kyc_sessions WHERE session_id = %s",
                            (b,)).fetchone()[0] == other
    assert send(kyc, uid, b, "Approved", int(time.time()), decision=approved_decision(b),
                workflow_id=other) == "applied" and me_approved(kyc, uid)
    use_workflow(kyc, WF)  # back again: B is another workflow, A's old state belongs to the previous epoch
    me = kyc.get(ME).json()
    assert (me["approved"], me["state"], me["can_start"]) == (False, "reverification_required", True)
    # A NEW signed approval of A, dated in this epoch, counts (Didit decided it after the switch).
    assert send(kyc, uid, a, "Approved", int(time.time()) + 5, decision=approved_decision(a)) == "applied"
    assert me_approved(kyc, uid)


# ── ownership ───────────────────────────────────────────────────────────────────

def test_verification_belongs_to_the_user_and_never_moves_with_a_creator(kyc):
    headers, a = owner(kyc, "kyc-a", slug="authtest-kyc-xfer")
    sid = start(kyc, headers)["session_id"]
    hook(kyc, session_id=sid, vendor=a, status="Approved", created_at=int(time.time()), decision=approved_decision(sid))
    b = make_user("kyc-b" + DOMAIN)
    with db() as conn:
        conn.execute("UPDATE public.streamers SET user_id = %s WHERE streamer_slug = 'authtest-kyc-xfer'", (b,))
        block = conn.execute("SELECT public.creator_donation_block(id) FROM public.streamers "
                             "WHERE streamer_slug = 'authtest-kyc-xfer'").fetchone()[0]
    assert approved(a) and not approved(b)  # the new owner inherits nothing
    assert block == "kyc_required"  # PR1's rule stands (PR3 changes no eligibility)
    assert kyc.get(ME).json()["approved"] is True  # A keeps their own verification
    assert asyncio.run(kyc.app_state.kyc.kyc_approved(a)) is True


# ── admin ───────────────────────────────────────────────────────────────────────

def test_admin_view_refresh_never_approves_and_no_manual_approval(kyc):
    headers, uid = owner(kyc, "kyc-adm")
    sid = start(kyc, headers)["session_id"]
    boss = new_browser(kyc)
    bh = signed_in(boss, "kyc-boss" + DOMAIN, role="admin")
    assert kyc.get(f"/api/v1/admin/users/{uid}/kyc").status_code == 403  # the owner is not an admin
    view = boss.get(f"/api/v1/admin/users/{uid}/kyc").json()
    assert view["sessions"][0]["session_id"] == sid and set(view["sessions"][0]) == {
        "session_id", "workflow_id", "workflow_version", "environment", "provider_status", "provider_updated_at",
        "check_statuses", "approved_at", "created_at", "updated_at"}
    kyc.fake.decisions[sid] = approved_decision(sid)
    r = boss.post(f"/api/v1/admin/users/{uid}/kyc/refresh", headers=bh)
    assert r.status_code == 200 and r.json()["refresh"] == "approval_needs_webhook"  # unsigned: never approves
    assert r.json()["approved"] is False and not approved(uid) and session_row(sid)[0] == "Not Started"
    assert "DOC-SECRET-77" not in r.text
    kyc.fake.decisions[sid] = {"session_id": sid, "status": "In Review", "id_verifications": [{"status": "In Review"}]}
    r = boss.post(f"/api/v1/admin/users/{uid}/kyc/refresh", headers=bh)
    assert r.json()["refresh"] == "applied" and r.json()["state"] == "in_review"
    assert [e["action"] for e in r.json()["events"]] == ["session_created", "admin_refresh"]
    for method, path in (("patch", f"/api/v1/admin/users/{uid}/kyc"), ("put", f"/api/v1/admin/users/{uid}/kyc"),
                         ("post", f"/api/v1/admin/users/{uid}/kyc")):
        assert getattr(boss, method)(path, headers=bh, json={"approved": True}).status_code in (404, 405)


def test_a_stale_refresh_never_overrides_a_newer_webhook_or_restores_approval(kyc):
    headers, uid = owner(kyc, "kyc-refresh-order")
    sid = start(kyc, headers)["session_id"]
    boss = new_browser(kyc)
    bh = signed_in(boss, "kyc-boss2" + DOMAIN, role="admin")
    refresh = lambda: boss.post(f"/api/v1/admin/users/{uid}/kyc/refresh", headers=bh).json()  # noqa: E731
    t = int(time.time())
    assert hook(kyc, session_id=sid, vendor=uid, status="Approved", created_at=t - 30,
                decision=approved_decision(sid)).json()["status"] == "applied" and approved(uid)
    assert hook(kyc, session_id=sid, vendor=uid, status="Declined", created_at=t - 20).json()["status"] == "applied"
    assert not approved(uid)
    kyc.fake.decisions[sid] = approved_decision(sid)  # a delayed answer that still shows the old approval
    assert refresh()["refresh"] == "approval_needs_webhook" and not approved(uid)

    async def newer_change_while_asking(session_id):
        """Didit is asked, then its state changes (Kyc Expired) and that webhook lands before the refresh writes;
        the refresh still reports the older state it was given. The change lies strictly between asking and
        writing, so only the asking time orders it correctly."""
        time.sleep(1.1)
        with db() as conn:
            assert deliver(conn, session_id=sid, vendor=uid, status="Kyc Expired",
                           created_at=int(time.time())) == "applied"
        time.sleep(1.1)
        return {"session_id": sid, "status": "In Review"}

    kyc.fake.decision = newer_change_while_asking
    assert refresh()["refresh"] == "stale" and session_row(sid)[0] == "Kyc Expired"  # dated when Didit was asked
    assert hook(kyc, session_id=sid, vendor=uid, status="Approved", created_at=int(time.time()) + 30,
                decision=approved_decision(sid)).json()["status"] == "applied"
    assert approved(uid)  # a valid, later, signed re-approval still applies


# ── early webhooks racing their session (both orders forced with an open transaction) ──

def test_a_webhook_racing_its_uncommitted_session_is_applied(kyc):
    _, uid = owner(kyc, "kyc-race-s")
    sid, result = str(uuid.uuid4()), {}
    with db() as conn:
        with conn.transaction():
            assert conn.execute("SELECT public.kyc_create_session(%s, %s, %s, 1, 'sandbox')",
                                (uid, sid, WF)).fetchone()[0] == "ok"
            th = threading.Thread(target=lambda: result.update(r=hook(
                kyc, session_id=sid, vendor=uid, status="In Review", created_at=int(time.time())).json()))
            th.start()
            wait_for_a_lock_waiter()  # the webhook waits for the session's transaction instead of missing it
            assert th.is_alive()
        th.join(20)
    assert result["r"] == {"status": "applied"} and session_row(sid)[0] == "In Review"
    with db() as conn:
        assert [r[0] for r in conn.execute("SELECT outcome FROM public.kyc_webhook_events WHERE session_id = %s",
                                           (sid,))] == ["applied"]


def test_a_session_racing_its_uncommitted_early_webhook_applies_it(kyc, env):
    _, uid = owner(kyc, "kyc-race-w")
    sid, result = str(uuid.uuid4()), {}

    def store():
        from app.cli import connection_factory
        from app.core.config import Settings
        created = CreatedSession(sid, "https://verify.didit.me/s/x", "Not Started", 1)
        result["row"] = asyncio.run(KycRepository(connection_factory(Settings())).record_session(uid, created, WF,
                                                                                               "sandbox"))

    with db() as conn:
        with conn.transaction():
            assert deliver(conn, session_id=sid, vendor=uid, status="In Review",
                           created_at=int(time.time())) == "unmatched"
            th = threading.Thread(target=store)
            th.start()
            wait_for_a_lock_waiter()  # the session waits for the webhook's transaction, then sees its receipt
            assert th.is_alive()
        th.join(20)
    assert result["row"]["provider_status"] == "In Review" and session_row(sid)[0] == "In Review"
    with db() as conn:
        assert [r[0] for r in conn.execute("SELECT outcome FROM public.kyc_webhook_events WHERE session_id = %s",
                                           (sid,))] == ["matched_later"]


# ── the API's database role: it can never forge or erase a verification ──────

def test_the_runtime_role_cannot_forge_an_approval_or_erase_history(kyc):
    headers, uid = owner(kyc, "kyc-role")
    sid = start(kyc, headers)["session_id"]
    t = int(time.time())
    with db() as conn:  # hyperchat_app, as the API connects
        for sql, args in (
                ("UPDATE public.kyc_sessions SET provider_status = 'Approved' WHERE session_id = %s", (sid,)),
                ("DELETE FROM public.kyc_sessions WHERE session_id = %s", (sid,)),
                ("INSERT INTO public.kyc_sessions (user_id, vendor_data, session_id, workflow_id, environment) "
                 "VALUES (%s, %s, gen_random_uuid(), %s, 'sandbox')", (uid, uid, WF)),
                ("SELECT public.kyc_record_provider_status(%s, 'Approved', now(), NULL, 'webhook', NULL)", (sid,)),
                ("INSERT INTO public.kyc_events (user_id, session_id, action, to_status, source) "
                 "VALUES (%s, %s, 'status_changed', 'Approved', 'webhook')", (uid, sid)),
                ("UPDATE public.kyc_events SET to_status = 'Approved' WHERE user_id = %s", (uid,)),
                ("DELETE FROM public.kyc_events WHERE user_id = %s", (uid,)),
                ("INSERT INTO public.kyc_webhook_events (event_id, webhook_type, session_id, vendor_data, workflow_id, "
                 "provider_status, provider_updated_at, environment, outcome) VALUES ('authtest-forged', "
                 "'status.updated', %s, %s, %s, 'Approved', now(), 'sandbox', 'unmatched')", (sid, uid, WF)),
                ("UPDATE public.kyc_webhook_events SET outcome = 'unmatched' WHERE session_id = %s", (sid,)),
                ("DELETE FROM public.kyc_webhook_events WHERE session_id = %s", (sid,)),
                ("SELECT webhook_secret FROM public.kyc_provider_config", ()),
                ("UPDATE public.kyc_provider_config SET webhook_secret = 'attacker-chosen-secret'", ()),
                ("SELECT public.kyc_hmac_sha256('\\x00', '\\x00')", ())):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql, args)
        # The functions it may call cannot be talked into an approval either.
        forged = envelope(session_id=sid, vendor=uid, status="Approved", created_at=t, decision=approved_decision(sid))
        text = didit.canonical(forged).decode()
        for signature in (sign(forged, "attacker-guess-0123456789"), "0" * 64, None,
                          sign({**forged, "status": "Declined"})):  # a genuine-looking signature of another body
            assert conn.execute("SELECT public.kyc_receive_webhook(%s, %s, %s)",
                                (text, signature, str(t))).fetchone()[0] == "bad_signature"
        assert conn.execute("SELECT public.kyc_apply_refresh(%s, 'Approved', '{}'::jsonb, clock_timestamp(), NULL)",
                            (sid,)).fetchone()[0] == "approval_needs_webhook"
        with pytest.raises(psycopg.errors.CheckViolation):  # nor post-date a refresh to outrank later webhooks
            conn.execute("SELECT public.kyc_apply_refresh(%s, 'Declined', '{}'::jsonb, "
                         "clock_timestamp() + interval '1 hour', NULL)", (sid,))
        for workflow, environment in ((str(uuid.uuid4()), "sandbox"), (WF, "live")):  # a weaker workflow / other env
            assert conn.execute("SELECT public.kyc_create_session(%s, gen_random_uuid(), %s, 1, %s)",
                                (uid, workflow, environment)).fetchone()[0] == "wrong_configuration"
        with pytest.raises(psycopg.errors.FeatureNotSupported), conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.execute("SELECT public.kyc_create_session(%s, gen_random_uuid(), %s, 1, 'sandbox')", (uid, WF))
        with pytest.raises((psycopg.errors.ForeignKeyViolation,  # KYC history keeps its user (and a creator
                            psycopg.errors.InsufficientPrivilege)):  # its owner: migration 0027)
            conn.execute("DELETE FROM public.users WHERE id = %s", (uid,))
    assert not approved(uid) and session_row(sid)[0] == "Not Started"
    # The genuine path still works, and a captured delivery replayed through the function is a duplicate.
    genuine = envelope(session_id=sid, vendor=uid, status="Approved", created_at=t, decision=approved_decision(sid))
    args = (didit.canonical(genuine).decode(), sign(genuine), str(t))
    with db() as conn:
        assert conn.execute("SELECT public.kyc_receive_webhook(%s, %s, %s)", args).fetchone()[0] == "applied"
        assert conn.execute("SELECT public.kyc_receive_webhook(%s, %s, %s)", args).fetchone()[0] == "duplicate"
    assert approved(uid)
    with fixture_db() as conn:  # the schema owner can (synthetic cleanup); the vendor rule holds for it too
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO public.kyc_sessions (user_id, vendor_data, session_id, workflow_id, environment) "
                         "VALUES (%s, 'someone', gen_random_uuid(), %s, 'sandbox')", (uid, WF))
        assert conn.execute("SELECT count(*) FROM public.kyc_sessions WHERE user_id = %s", (uid,)).fetchone()[0] == 1


def test_existing_donations_and_eligibility_are_unchanged(kyc):
    headers, uid = owner(kyc, "kyc-pay", slug="authtest-kyc-pay")
    with db() as conn:
        sid_creator = conn.execute("SELECT id::text FROM public.streamers WHERE streamer_slug = 'authtest-kyc-pay'"
                                   ).fetchone()[0]
    make_donation(sid_creator, 100, moderation="approved")
    with db() as conn:
        before = conn.execute("SELECT public.creator_donation_block(%s), (SELECT md5(string_agg(to_jsonb(d)::text, ',')) "
                              "FROM public.donations d WHERE streamer_id = %s)", (sid_creator, sid_creator)).fetchone()
    sid = start(kyc, headers)["session_id"]
    hook(kyc, session_id=sid, vendor=uid, status="Approved", created_at=int(time.time()), decision=approved_decision(sid))
    hook(kyc, session_id=sid, vendor=uid, status="Kyc Expired", created_at=int(time.time()) + 5)
    with db() as conn:
        after = conn.execute("SELECT public.creator_donation_block(%s), (SELECT md5(string_agg(to_jsonb(d)::text, ',')) "
                             "FROM public.donations d WHERE streamer_id = %s)", (sid_creator, sid_creator)).fetchone()
    assert after == before == (None, before[1])  # legacy creator still eligible before enforcement (PR4)
