"""Phase 10 on PostgreSQL (scratch database, see tests/integration.py):

  * the claim: eligibility, order, audio_played_at, the 24 h window, unservable items skipped, hidden messages,
    Retry-After, audio-now-playing in the SAME transaction, concurrent claimers never sharing an item
  * replay: rules, audit row, events, scoping
  * OBS tokens: create (hash only, shown once), rotation grace, revoke, list, migrated tokens
  * "!vaani": payment completion on every path, credit, no audio, the celebration event
  * the API end to end: session, claim, Media Source 302, token endpoints, replay endpoint, authorization, CSRF,
    Origin, IDOR, rate limits, and no token in any log line
"""

import asyncio
import secrets
from datetime import UTC, datetime

import pytest

from app.obs.queue import AudioQueue, NotReplayable
from app.obs.tokens import TokenRepository
from app.payments.repository import PaymentRepository
from app.realtime.outbox import OutboxQueue
from app.security.crypto import sha256_hex
from app.security.pg_stores import PostgresObsTokenStore
from tests.integration import DOMAIN, ORIGIN, db, login, make_donation, make_streamer, make_user, requires_db, update_streamer
from tests.jobs.test_jobs_db import factory, run

pytestmark = requires_db
BASE = "https://media.example.test"


def tts(sid, did):
    return f"{BASE}/tts-audio/{sid}/{did}.mp3"


def queued(sid, *, minutes_ago=1.0, visible=True, url=None, moderation="approved", payment="success", name="Ravi",
           message="hello there", **cols) -> str:
    did = make_donation(sid, 100, payment=payment, moderation=moderation, name=name, message=message, visible=visible)
    with db() as conn:
        conn.execute("UPDATE public.donations SET audio_scheduled_at = now() - make_interval(secs => %s), "
                     "tts_audio_url = %s WHERE id = %s", (minutes_ago * 60, url if url is not None else tts(sid, did), did))
        for k, v in cols.items():
            conn.execute(f"UPDATE public.donations SET {k} = %s WHERE id = %s", (v, did))
    return did


def played(did):
    with db() as conn:
        return conn.execute("SELECT audio_played_at FROM public.donations WHERE id = %s", (did,)).fetchone()[0]


def outbox_rows(sid):
    with db() as conn:
        return conn.execute("SELECT event, subject_id::text, facts, created_at FROM public.realtime_outbox "
                            "WHERE streamer_id = %s ORDER BY id", (sid,)).fetchall()


def claim(sid, slug):
    return run(AudioQueue(factory()).claim(sid, slug, BASE))


@pytest.fixture
def clean(env):
    yield


# ── the claim ───────────────────────────────────────────────────────────────

def test_claim_order_marking_and_event_in_the_same_transaction(clean):
    slug = "authtest-p10-order"
    sid = make_streamer(slug)
    later, first = queued(sid, minutes_ago=1), queued(sid, minutes_ago=5)
    r = claim(sid, slug)
    assert r.item.donation["id"] == first and r.item.kind == "tts" and r.item.audio_url == tts(sid, first)
    assert played(first) is not None and played(later) is None
    ((event, subject, facts, created),) = outbox_rows(sid)
    assert (event, subject, facts) == ("audio-now-playing", first, {"kind": "tts"}) and created == played(first)
    assert claim(sid, slug).item.donation["id"] == later
    empty = claim(sid, slug)
    assert empty.item is None and empty.retry_after is None


@pytest.mark.parametrize("cols, eligible", [
    ({}, True), ({"moderation": "auto_approved"}, True), ({"moderation": "pending"}, False),
    ({"moderation": "rejected"}, False), ({"payment": "pending"}, False), ({"payment": "failed"}, False),
    ({"minutes_ago": 23 * 60}, True), ({"minutes_ago": 25 * 60}, False),     # P10-D7: 24-hour window
    ({"audio_played_at": datetime.now(UTC)}, False), ({"audio_scheduled_at": None}, False),
])
def test_eligibility(clean, cols, eligible):
    slug = "authtest-p10-elig"
    sid = make_streamer(slug)
    did = queued(sid, **cols)
    r = claim(sid, slug)
    assert (r.item is not None and r.item.donation["id"] == did) is eligible


def test_no_audio_url_is_not_queued(clean):
    slug = "authtest-p10-noaudio"
    sid = make_streamer(slug)
    did = queued(sid)
    with db() as conn:
        conn.execute("UPDATE public.donations SET tts_audio_url = NULL WHERE id = %s", (did,))
    assert claim(sid, slug).item is None and played(did) is None


def test_unservable_items_are_marked_played_and_skipped(clean):
    slug = "authtest-p10-skip"
    sid = make_streamer(slug)
    legacy = queued(sid, minutes_ago=9, url="https://pub-3968c5ed4c4c448d85e9dc68e6552634.r2.dev/tts/x.mp3")
    foreign = queued(sid, minutes_ago=8, url=f"{BASE}/tts-audio/{sid}/00000000-0000-4000-8000-000000000000.mp3")
    good = queued(sid, minutes_ago=7)
    r = claim(sid, slug)
    assert r.item.donation["id"] == good and r.skipped == 2
    assert played(legacy) is not None and played(foreign) is not None
    assert [e[0] for e in outbox_rows(sid)] == ["audio-now-playing"]  # nothing announced for skipped items


def test_hidden_message_plays_silence_and_shows_no_message(clean):
    slug = "authtest-p10-hidden"
    sid = make_streamer(slug)
    queued(sid, visible=False, message="do not say this")
    r = claim(sid, slug)
    assert r.item.kind == "silence" and r.item.audio_url == f"{BASE}/system/silence.mp3"
    from app.obs.service import ObsService
    body = ObsService.claim_body(r)
    assert body["display"]["message"] is None and "do not say this" not in str(body)


def test_retry_after_for_the_next_due_item(clean):
    slug = "authtest-p10-due"
    sid = make_streamer(slug)
    soon = queued(sid, minutes_ago=-0.25)  # due in 15 s (e.g. the 10 s auto-approve delay)
    r = claim(sid, slug)
    assert r.item is None and 1 <= r.retry_after <= 16 and played(soon) is None
    with db() as conn:
        conn.execute("UPDATE public.donations SET audio_scheduled_at = now() + interval '5 minutes' WHERE id = %s",
                     (soon,))
    assert claim(sid, slug).retry_after is None


def test_other_creators_items_are_never_claimed(clean):
    a, b = make_streamer("authtest-p10-a"), make_streamer("authtest-p10-b")
    theirs = queued(b)
    assert claim(a, "authtest-p10-a").item is None and played(theirs) is None


def test_concurrent_claimers_never_share_an_item(clean):
    slug = "authtest-p10-race"
    sid = make_streamer(slug)
    ids = {queued(sid, minutes_ago=10 + i) for i in range(20)}

    async def go():
        q = AudioQueue(factory())
        got = []

        async def worker():
            while True:
                r = await q.claim(sid, slug, BASE)
                if r.item is None:
                    return
                got.append(r.item.donation["id"])
        await asyncio.gather(*(worker() for _ in range(8)))
        return got
    got = run(go())
    assert sorted(got) == sorted(ids) and len(got) == len(set(got)) == 20
    assert len([e for e in outbox_rows(sid) if e[0] == "audio-now-playing"]) == 20


def test_now_playing_events_expire_after_30_seconds(clean):
    slug = "authtest-p10-exp"
    sid = make_streamer(slug)
    queued(sid)
    claim(sid, slug)
    with db() as conn:
        conn.execute("INSERT INTO public.realtime_outbox (streamer_id, channel, event) VALUES (%s, 'settings', "
                     "'settings-updated')", (sid,))
        conn.execute("UPDATE public.realtime_outbox SET created_at = now() - interval '31 seconds' WHERE streamer_id = %s",
                     (sid,))
    run(OutboxQueue(factory()).expire())
    with db() as conn:
        states = dict(conn.execute("SELECT event, status FROM public.realtime_outbox WHERE streamer_id = %s",
                                   (sid,)).fetchall())
    assert states == {"audio-now-playing": "expired", "settings-updated": "pending"}


# ── replay ──────────────────────────────────────────────────────────────────

def test_replay_requeues_with_audit_and_events(clean):
    slug = "authtest-p10-replay"
    sid = make_streamer(slug)
    did = queued(sid)
    claim(sid, slug)
    run(AudioQueue(factory()).replay(sid, did, actor_user_id=None, actor_label="Dashboard"))
    assert played(did) is None
    with db() as conn:
        assert conn.execute("SELECT action_type, action_source, previous_status, new_status FROM "
                            "public.moderation_actions WHERE donation_id = %s", (did,)).fetchall() == \
            [("replay", "dashboard", "approved", "approved")]
    assert [e[0] for e in outbox_rows(sid)][-2:] == ["donation-updated", "audio-ready"]
    assert outbox_rows(sid)[-2][2] == {"action": "replay", "previous_status": "approved"}
    assert claim(sid, slug).item.donation["id"] == did  # plays again


@pytest.mark.parametrize("cols", [{"moderation": "rejected"}, {"moderation": "pending"}, {"payment": "pending"},
                                  {"url": ""}])
def test_replay_refuses_unapproved_or_silent_donations(clean, cols):
    slug = "authtest-p10-norep"
    sid = make_streamer(slug)
    did = queued(sid, **({k: v for k, v in cols.items() if k != "url"}))
    if "url" in cols:
        with db() as conn:
            conn.execute("UPDATE public.donations SET tts_audio_url = NULL WHERE id = %s", (did,))
    with pytest.raises(NotReplayable):
        run(AudioQueue(factory()).replay(sid, did, actor_user_id=None, actor_label="D"))
    assert outbox_rows(sid) == []


def test_replay_is_scoped_to_the_creator(clean):
    a, b = make_streamer("authtest-p10-ra"), make_streamer("authtest-p10-rb")
    theirs = queued(b)
    with pytest.raises(LookupError):
        run(AudioQueue(factory()).replay(a, theirs, actor_user_id=None, actor_label="D"))


# ── OBS tokens ──────────────────────────────────────────────────────────────

def test_token_lifecycle_rotation_grace_and_revoke(clean):
    sid = make_streamer("authtest-p10-tok")
    repo, store = TokenRepository(factory()), PostgresObsTokenStore(factory())
    first, row1, rotated = run(repo.create(sid))
    assert rotated == 0 and row1["state"] == "active"
    with db() as conn:  # only the hash is stored
        assert conn.execute("SELECT count(*) FROM public.obs_tokens WHERE token_hash = %s", (first,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM public.obs_tokens WHERE token_hash = %s",
                            (sha256_hex(first),)).fetchone()[0] == 1
    second, _, rotated = run(repo.create(sid))
    assert rotated == 1
    states = {r["id"]: r["state"] for r in run(repo.list(sid))}
    assert states[row1["id"]] == "revoking"
    assert run(store.validate(sha256_hex(first))) is not None  # still valid during the 60 s grace
    with db() as conn:
        conn.execute("UPDATE public.obs_tokens SET revoked_at = now() - interval '1 second' WHERE id = %s", (row1["id"],))
    assert run(store.validate(sha256_hex(first))) is None and run(store.validate(sha256_hex(second))) is not None
    listed = run(repo.list(sid))
    assert all(set(r) == {"id", "state", "created_at", "last_used_at", "usage_count", "expires_at", "revoked_at"}
               for r in listed)
    current = next(r for r in listed if r["state"] == "active")
    assert run(repo.revoke(sid, current["id"]))["state"] == "revoked"
    assert run(store.validate(sha256_hex(second))) is None
    other = make_streamer("authtest-p10-tok2")
    assert run(repo.revoke(other, row1["id"])) is None  # another creator's token: not found


def test_migrated_legacy_token_keeps_working(clean):
    sid = make_streamer("authtest-p10-legacy")
    legacy = "obs_" + secrets.token_hex(32)  # as the ETL stored it: the hash of the legacy plaintext
    with db() as conn:
        conn.execute("INSERT INTO public.obs_tokens (streamer_id, token_hash, is_active) VALUES (%s, %s, true)",
                     (sid, sha256_hex(legacy)))
    info = run(PostgresObsTokenStore(factory()).validate(sha256_hex(legacy)))
    assert info is not None and info.streamer_id == sid


# ── "!vaani" ────────────────────────────────────────────────────────────────

def pending_payment(sid, message, **cols):
    did = make_donation(sid, 150, payment="pending", moderation="pending", name="Vaani Fan", message=message)
    if cols:
        with db() as conn:
            for k, v in cols.items():
                conn.execute(f"UPDATE public.donations SET {k} = %s WHERE id = %s", (v, did))
    return did


def test_vaani_celebration_on_payment_completion(clean):
    slug = "authtest-p10-vaani"
    sid = make_streamer(slug)
    update_streamer(sid, moderation_mode="manual")  # forced auto-approval whatever the moderation mode
    did = pending_payment(sid, "  !VAANI ")
    repo = PaymentRepository(factory(), vaani_creators=frozenset({slug}))
    applied = run(repo.apply(did, "captured", "pay_TESTp10" + secrets.token_hex(4)))
    assert applied.result == "completed" and applied.auto_approved
    with db() as conn:
        d = conn.execute("SELECT moderation_status, audio_scheduled_at, tts_audio_url FROM public.donations WHERE id = %s",
                         (did,)).fetchone()
        jobs = conn.execute("SELECT count(*) FROM public.jobs WHERE dedupe_key = %s", (did,)).fetchone()[0]
        total = conn.execute("SELECT total_amount FROM public.streamer_donator_totals WHERE streamer_id = %s",
                             (sid,)).fetchone()
    assert d == ("auto_approved", None, None) and jobs == 0 and total is not None and float(total[0]) == 150.0
    assert [e[0] for e in outbox_rows(sid)] == ["new-donation", "goal-progress", "leaderboard-updated",
                                                "vaani-celebration"]
    assert claim(sid, slug).item is None  # never queued


@pytest.mark.parametrize("message, cols, configured", [
    ("!vaani please", {}, True), ("!vaani", {"voice_message_url": "https://x/y.webm"}, True),
    ("!vaani", {"hypersound_url": "https://x/h.mp3"}, True), ("!vaani", {}, False),
])
def test_not_a_vaani_celebration(clean, message, cols, configured):
    slug = "authtest-p10-novaani"
    sid = make_streamer(slug)
    did = pending_payment(sid, message, **cols)
    repo = PaymentRepository(factory(), vaani_creators=frozenset({slug} if configured else set()))
    run(repo.apply(did, "captured", "pay_TESTp10" + secrets.token_hex(4)))
    assert "vaani-celebration" not in [e[0] for e in outbox_rows(sid)]


# ── the API end to end ──────────────────────────────────────────────────────

@pytest.fixture
def obs(app):
    app.app_state.obs.public_base = BASE  # the scratch app has no R2; claims only need the public base
    yield app


def owner(client, slug, email):
    sid = make_streamer(slug, user_id=make_user(email))
    r = login(client, email)
    assert r.status_code == 200
    return sid, {**ORIGIN, "x-csrf-token": r.json()["csrf_token"]}


def test_token_endpoints(obs):
    slug = "authtest-p10-api"
    sid, h = owner(obs, slug, "p10owner" + DOMAIN)
    base = f"/api/v1/dashboard/streamers/{slug}/obs-tokens"
    assert obs.post(base, headers=ORIGIN).status_code == 403  # no CSRF
    r = obs.post(base, headers=h)
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    body = r.json()
    token = body["player_url"].split("#token=")[1]
    assert body["player_url"].startswith("https://testserver/obs/player#token=obs_")
    assert body["media_source_url"] == f"https://testserver/api/v1/obs/audio/next?token={token}"
    assert set(body["token"]) == {"id", "state", "created_at", "last_used_at", "usage_count", "expires_at",
                                  "revoked_at"}
    listed = obs.get(base).json()["tokens"]
    assert token not in str(listed) and sha256_hex(token) not in str(listed)
    assert obs.delete(f"{base}/not-a-uuid", headers=h).status_code == 404
    assert obs.delete(f"{base}/{body['token']['id']}", headers=h).json()["token"]["state"] == "revoked"
    obs.cookies.clear()
    _, h2 = owner(obs, "authtest-p10-api2", "p10other" + DOMAIN)
    assert obs.get(base).status_code == 403 and obs.post(base, headers=h2).status_code == 403  # another creator
    other = obs.post("/api/v1/dashboard/streamers/authtest-p10-api2/obs-tokens", headers=h2).json()
    obs.cookies.clear()
    owner_login = login(obs, "p10owner" + DOMAIN)
    h = {**ORIGIN, "x-csrf-token": owner_login.json()["csrf_token"]}
    assert obs.delete(f"{base}/{other['token']['id']}", headers=h).status_code == 404  # IDOR: not mine


def test_token_creation_rate_limit(obs):
    slug = "authtest-p10-rl"
    _, h = owner(obs, slug, "p10rl" + DOMAIN)
    codes = [obs.post(f"/api/v1/dashboard/streamers/{slug}/obs-tokens", headers=h).status_code for _ in range(11)]
    assert codes == [201] * 10 + [429]


def obs_token(sid) -> str:
    token = "obs_" + secrets.token_hex(32)
    with db() as conn:
        conn.execute("INSERT INTO public.obs_tokens (streamer_id, token_hash, is_active) VALUES (%s, %s, true)",
                     (sid, sha256_hex(token)))
    return token


def test_session_claim_and_media_source(obs):
    slug = "authtest-p10-play"
    sid = make_streamer(slug, name="P10 Play")
    token = obs_token(sid)
    hdr = {"x-obs-token": token}
    s = obs.get("/api/v1/obs/session", headers=hdr)
    assert s.status_code == 200 and s.json()["streamer"] == {"slug": slug, "name": "P10 Play"}
    assert s.json()["realtime"] is None and s.json()["silence_url"] == f"{BASE}/system/silence.mp3"
    assert s.headers["cache-control"] == "no-store"
    assert obs.get("/api/v1/obs/session").status_code == 401
    assert obs.get("/api/v1/obs/session", headers={**hdr, "origin": "https://evil.example"}).status_code == 403
    assert obs.get("/api/v1/obs/session", headers={**hdr, "origin": "https://testserver"}).status_code == 200
    first, second = queued(sid, minutes_ago=3), queued(sid, minutes_ago=2, visible=False)
    r = obs.post("/api/v1/obs/audio/claim", headers=hdr)
    assert r.status_code == 200 and r.json()["donation_id"] == first and r.json()["kind"] == "tts"
    assert set(r.json()) == {"donation_id", "kind", "audio_url", "display"}
    nxt = obs.get("/api/v1/obs/audio/next", params={"token": token}, follow_redirects=False)
    assert nxt.status_code == 302 and nxt.headers["location"] == f"{BASE}/system/silence.mp3"  # hidden: silence
    assert nxt.headers["cache-control"] == "no-store"
    empty = obs.post("/api/v1/obs/audio/claim", headers=hdr)
    assert empty.status_code == 204 and "retry-after" not in empty.headers
    assert obs.get("/api/v1/obs/audio/next", params={"token": token}, follow_redirects=False).status_code == 204
    assert played(second) is not None
    assert obs.post("/api/v1/obs/audio/claim", headers={"x-obs-token": "obs_" + "0" * 64}).status_code == 401


def test_claim_rate_limit(obs):
    sid = make_streamer("authtest-p10-crl")
    hdr = {"x-obs-token": obs_token(sid)}
    codes = [obs.post("/api/v1/obs/audio/claim", headers=hdr).status_code for _ in range(122)]
    assert codes[:120] == [204] * 120 and codes[120:] == [429, 429]


def test_replay_endpoint(obs):
    slug = "authtest-p10-rep"
    sid, h = owner(obs, slug, "p10rep" + DOMAIN)
    did = queued(sid)
    claim(sid, slug)
    url = f"/api/v1/dashboard/streamers/{slug}/donations/{did}/replay"
    assert obs.post(url, headers=ORIGIN).status_code == 403  # CSRF
    assert obs.post(url, headers={**h, "origin": "https://evil.example"}).status_code == 403
    r = obs.post(url, headers=h)
    assert r.status_code == 200 and r.json() == {"result": "queued", "donation_id": did} and played(did) is None
    rejected = queued(sid, moderation="rejected")
    assert obs.post(f"/api/v1/dashboard/streamers/{slug}/donations/{rejected}/replay", headers=h).status_code == 409
    assert obs.post(f"/api/v1/dashboard/streamers/{slug}/donations/nope/replay", headers=h).status_code == 404
    other = make_streamer("authtest-p10-rep2")
    theirs = queued(other)
    assert obs.post(f"/api/v1/dashboard/streamers/{slug}/donations/{theirs}/replay", headers=h).status_code == 404
    assert obs.post(f"/api/v1/dashboard/streamers/authtest-p10-rep2/donations/{theirs}/replay",
                    headers=h).status_code == 403


def test_no_tokens_in_logs(obs):
    slug = "authtest-p10-log"
    sid, h = owner(obs, slug, "p10log" + DOMAIN)
    created = obs.post(f"/api/v1/dashboard/streamers/{slug}/obs-tokens", headers=h).json()
    token = created["player_url"].split("#token=")[1]
    queued(sid)
    obs.get("/api/v1/obs/session", headers={"x-obs-token": token})
    obs.get("/api/v1/obs/audio/next", params={"token": token}, follow_redirects=False)
    obs.post("/api/v1/obs/audio/claim", params={"token": token})
    text = "\n".join(obs.app_state.log_capture.records)
    assert token not in text and sha256_hex(token) not in text and "hello there" not in text and "Ravi" not in text
    assert "obs audio claimed" in text and "obs token created" in text
