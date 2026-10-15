"""KYC PR3 (migration 0022) through the real app on the scratch database: owner session API with a fake Didit (no
real call), signed webhooks (signature, freshness, replay, identity, ordering, concurrency), the approval rule,
ownership, admin views / refresh, database roles, and that nothing else changes (no activation, no eligibility
change). Synthetic rows only."""

import asyncio
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

    def __init__(self) -> None:
        self.workflow_id = WF
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


@pytest.fixture
def kyc(app):
    """The app's KYC service, configured for a sandbox application with a fake Didit."""
    settings = app.app_state.settings.model_copy(update={
        "didit_api_key": SecretStr("test-key"), "didit_webhook_secret": SecretStr(SECRET), "didit_workflow_id": WF,
        "didit_environment": "sandbox", "didit_application_id": APP_ID})
    fake = FakeDidit()
    app.app_state.kyc = KycService(app.app_state.kyc.repo, settings, client=fake, limiter=app.app_state.auth.repo)
    app.fake = fake
    return app


def owner(client, name: str, slug: str | None = None) -> tuple[dict, str]:
    """A signed-in user who owns a (legacy, existing-style) creator: (CSRF headers, user id)."""
    headers = signed_in(client, name + DOMAIN)
    with db() as conn:
        uid = conn.execute("SELECT id::text FROM public.users WHERE email = %s", (name + DOMAIN,)).fetchone()[0]
    make_streamer(slug or f"authtest-{name}", user_id=uid)
    return headers, uid


def hook(app, *, session_id, vendor, status, created_at, event_id=None, decision=None, secret=SECRET, ts=None,
         x_ts=None, sign=True, **extra):
    now = int(time.time())
    body = {"event_id": event_id or f"authtest-ev-{uuid.uuid4().hex[:12]}", "webhook_type": "status.updated",
            "timestamp": now if ts is None else ts, "created_at": created_at, "application_id": APP_ID,
            "environment": "sandbox", "session_id": session_id, "status": status, "workflow_id": WF,
            "workflow_version": 1, "vendor_data": vendor, "metadata": None, **extra}
    if decision is not None:
        body["decision"] = decision
    headers = {"content-type": "application/json", "x-timestamp": str(now if x_ts is None else x_ts)}
    if sign:
        headers["x-signature-v2"] = didit.sign(body, secret)
    return app.post(HOOK, content=json.dumps(body), headers=headers)


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
    for kw, code in (({"sign": False}, 401), ({"secret": "wrong-secret"}, 401), ({"ts": now - 301}, 401),
                     ({"x_ts": now - 301}, 401), ({"ts": "1774970000"}, 401), ({"environment": "live"}, 403),
                     ({"application_id": "00000000-0000-4000-8000-000000000000"}, 403)):
        assert hook(kyc, **common, **kw).status_code == code, kw
    tampered = {"event_id": "authtest-ev-t", "webhook_type": "status.updated", "timestamp": now, "created_at": now,
                "application_id": APP_ID, "environment": "sandbox", "session_id": sid, "status": "Declined",
                "workflow_id": WF, "vendor_data": uid}
    signature = didit.sign(tampered, SECRET)
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
                                                 (future,))) == ["matched_later", "unmatched"]


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

def test_admin_view_refresh_and_no_manual_approval(kyc):
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
    assert r.status_code == 200 and r.json()["approved"] is True and approved(uid)
    assert [e["action"] for e in r.json()["events"]] == ["session_created", "admin_refresh"]
    assert "DOC-SECRET-77" not in r.text
    for method, path in (("patch", f"/api/v1/admin/users/{uid}/kyc"), ("put", f"/api/v1/admin/users/{uid}/kyc"),
                         ("post", f"/api/v1/admin/users/{uid}/kyc")):
        assert getattr(boss, method)(path, headers=bh, json={"approved": True}).status_code in (404, 405)


# ── database roles ──────────────────────────────────────────────────────────────

def test_the_runtime_role_cannot_write_outcomes_directly(kyc):
    headers, uid = owner(kyc, "kyc-role")
    sid = start(kyc, headers)["session_id"]
    with db() as conn:
        for sql, args in (
                ("UPDATE public.kyc_sessions SET provider_status = 'Approved' WHERE session_id = %s", (sid,)),
                ("DELETE FROM public.kyc_sessions WHERE session_id = %s", (sid,)),
                ("INSERT INTO public.kyc_sessions (user_id, vendor_data, session_id, workflow_id, environment, "
                 "provider_status) VALUES (%s, %s, gen_random_uuid(), %s, 'sandbox', 'Approved')", (uid, uid, WF)),
                ("UPDATE public.kyc_events SET to_status = 'Approved' WHERE user_id = %s", (uid,)),
                ("DELETE FROM public.kyc_events WHERE user_id = %s", (uid,)),
                ("UPDATE public.kyc_webhook_events SET session_id = NULL WHERE session_id = %s", (sid,))):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql, args)
        with pytest.raises(psycopg.errors.CheckViolation):  # vendor_data must be the user
            conn.execute("INSERT INTO public.kyc_sessions (user_id, vendor_data, session_id, workflow_id, environment) "
                         "VALUES (%s, 'someone', gen_random_uuid(), %s, 'sandbox')", (uid, WF))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):  # KYC history keeps its user
            conn.execute("DELETE FROM public.users WHERE id = %s", (uid,))
    assert not approved(uid)
    with fixture_db() as conn:  # the schema owner can (synthetic cleanup)
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
