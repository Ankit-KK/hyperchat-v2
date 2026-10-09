"""Phase 11 on PostgreSQL (scratch database, see tests/integration.py), Telegram and Discord mocked:

  * the moderator_notify job is enqueued in the payment-completion transaction (pending: now; auto-approved: when
    its audio is due), never for failed payments or !vaani, and rolls back with the payment
  * the historical backlog (paid, mod_notified = false, never enqueued) is never sent; a job for a donation older
    than 24 h sends nothing
  * the job end to end: Telegram DM + Discord channel post, delivery rows, callback rows, an idempotent retry that
    only re-sends what failed, the recipient allowlist, mod_notified
  * both webhooks through the real app: authentication, the shared moderation core (audit source telegram /
    discord, moderator stats, realtime events, audio job), deduplication, cross-moderator refusal, and logs
"""

import json
import logging
import secrets
import time

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from app.bots.clients import DiscordClient, TelegramClient
from app.bots.notify import ModeratorNotifyHandler
from app.bots.repository import BotRepository
from app.core.config import get_settings
from app.jobs.queue import JobQueue
from app.jobs.runner import Worker
from app.payments.repository import PaymentRepository
from tests.integration import CaptureSender, FakeGoogle, LogCapture, db, make_donation, make_streamer, requires_db, \
    update_streamer
from tests.jobs.test_jobs_db import factory, run

pytestmark = requires_db
TOKEN = "123456:TEST-ONLY-" + secrets.token_hex(8)
SECRET = "TEST-ONLY-secret-" + secrets.token_hex(8)
DTOKEN = "TEST-ONLY-discord-" + secrets.token_hex(8)
MESSAGE = "a private donor message " + secrets.token_hex(3)
TG_MOD, TG_OTHER = "7000000001", "7000000002"
DC_MOD, DC_CHANNEL = "700000000000000001", "700000000000000099"


@pytest.fixture
def clean(env):
    yield


def jobs(did):
    with db() as conn:
        return conn.execute("SELECT kind, status, available_at - created_at > interval '5 seconds' "
                            "FROM public.jobs WHERE dedupe_key = %s ORDER BY kind", (did,)).fetchall()


def notified(did):
    with db() as conn:
        return conn.execute("SELECT mod_notified FROM public.donations WHERE id = %s", (did,)).fetchone()[0]


def deliveries(did):
    with db() as conn:
        return conn.execute("SELECT platform, target_kind, target_id, status, last_error FROM "
                            "public.moderator_notifications WHERE donation_id = %s ORDER BY platform", (did,)).fetchall()


def creator(slug, **cols):
    sid = make_streamer(slug)
    update_streamer(sid, **cols) if cols else None
    return sid


def add_moderator(sid, *, telegram=None, discord=None, **perms) -> str:
    cols = {"telegram_user_id": telegram, "discord_user_id": discord, **perms}
    keys = ", ".join(cols)
    with db() as conn:
        return conn.execute(f"INSERT INTO public.streamers_moderators (streamer_id, mod_name, {keys}) "
                            f"VALUES (%s, 'Mod One', {', '.join(['%s'] * len(cols))}) RETURNING id::text",
                            (sid, *cols.values())).fetchone()[0]


def pending_payment(sid, message=MESSAGE):
    return make_donation(sid, 100, payment="pending", moderation="pending", name="Ravi", message=message)


def pay(did, vaani=frozenset()):
    return run(PaymentRepository(factory(), vaani_creators=vaani).apply(did, "captured", "pay_TEST" + secrets.token_hex(4)))


# ── enqueue boundary ────────────────────────────────────────────────────────

def test_notify_job_is_enqueued_with_the_payment(clean):
    manual = creator("authtest-p11-manual", moderation_mode="manual")
    auto = creator("authtest-p11-auto")
    d1, d2, d3 = pending_payment(manual), pending_payment(auto), pending_payment(manual)
    pay(d1)
    pay(d2)
    run(PaymentRepository(factory()).apply(d3, "failed", None))
    assert jobs(d1) == [("moderator_notify", "queued", False)]                       # pending: now
    assert ("moderator_notify", "queued", True) in jobs(d2)                          # auto: when its audio is due
    assert jobs(d3) == []                                                            # failed payment: nothing
    v = pending_payment(manual, "!vaani")
    pay(v, frozenset({"authtest-p11-manual"}))
    assert jobs(v) == []                                                             # !vaani: never notified


def test_notify_job_rolls_back_with_the_payment(clean, monkeypatch):
    sid = creator("authtest-p11-rb")
    did = pending_payment(sid)

    async def broken(*a, **k):
        raise psycopg.errors.CheckViolation("injected")
    monkeypatch.setattr("app.payments.repository.outbox.emit", broken)
    with pytest.raises(psycopg.Error):
        pay(did)
    assert jobs(did) == []


# ── the job ─────────────────────────────────────────────────────────────────

class Platforms:
    """Mocked Telegram + Discord: records every send; `fail` maps a platform to statuses for its next sends."""

    def __init__(self, fail=None):
        self.sent, self.fail = [], {k: list(v) for k, v in (fail or {}).items()}

    def transport(self, platform):
        def handler(request):
            body = json.loads(request.content) if request.content else {}
            statuses = self.fail.get(platform) or []
            status = statuses.pop(0) if statuses else 200
            self.sent.append((platform, request.url.path, body, status))
            if platform == "telegram":
                return httpx.Response(status, json={"ok": status == 200, "result": {"message_id": len(self.sent)}})
            if request.url.path.endswith("/users/@me/channels"):
                return httpx.Response(status, json={"id": "700000000000000555"})
            return httpx.Response(status, json={"id": str(len(self.sent))})
        return httpx.MockTransport(handler)

    def handler(self, *, enabled=True, recipients=None):
        h = ModeratorNotifyHandler(
            BotRepository(factory()), telegram=TelegramClient(TOKEN, self.transport("telegram")),
            discord=DiscordClient(DTOKEN, self.transport("discord")), enabled=enabled, recipients=recipients,
            frontend_url="https://app.example.test", public_base="https://media.example.test")
        h.backoff = type(h.backoff)(0.01, 0.02)
        return h


def work(handler, did, rounds=40):
    async def scenario():
        import asyncio
        w = Worker(JobQueue(factory()), {handler.kind: handler}, poll_seconds=0.01)
        for _ in range(rounds):
            await w.run_once()
            with db() as conn:
                row = conn.execute("SELECT status FROM public.jobs WHERE dedupe_key = %s AND kind = 'moderator_notify'",
                                   (did,)).fetchone()
            if row and row[0] in ("succeeded", "failed"):
                return
            await asyncio.sleep(0.03)
    run(scenario())


def test_historical_backlog_is_never_sent(clean):
    sid = creator("authtest-p11-backlog", moderation_mode="manual")
    add_moderator(sid, telegram=TG_MOD)
    old = make_donation(sid, 100, payment="success", moderation="pending", message=MESSAGE)
    with db() as conn:  # a paid, never-notified donation from before Phase 11 (2 days old)
        conn.execute("UPDATE public.donations SET created_at = now() - interval '2 days' WHERE id = %s", (old,))
    platforms = Platforms()
    work(platforms.handler(), old, rounds=3)  # nothing scans mod_notified = false: no job exists for it
    assert jobs(old) == [] and platforms.sent == [] and notified(old) is False
    run(JobQueue(factory()).enqueue("moderator_notify", old, {"donation_id": old}))  # even if one were queued
    work(platforms.handler(), old)
    assert platforms.sent == [] and deliveries(old) == [] and notified(old) is True  # too old: nothing sent


def test_job_end_to_end_retry_only_resends_what_failed(clean):
    sid = creator("authtest-p11-e2e", moderation_mode="manual", discord_moderation_enabled=True,
                  discord_moderation_channel_id=DC_CHANNEL)
    tg_mod = add_moderator(sid, telegram=TG_MOD)
    add_moderator(sid, discord=DC_MOD, can_ban=True)
    did = pending_payment(sid)
    pay(did)
    platforms = Platforms(fail={"discord": [502]})  # Discord's first post fails transiently
    work(platforms.handler(), did)
    sends = [(p, path) for p, path, _, status in platforms.sent]
    assert sends.count(("telegram", f"/bot{TOKEN}/sendMessage")) == 1  # never re-sent on the retry
    assert sends.count(("discord", f"/api/v10/channels/{DC_CHANNEL}/messages")) == 2  # failed once, then sent
    assert deliveries(did) == [("discord", "channel", DC_CHANNEL, "sent", None), ("telegram", "dm", TG_MOD, "sent", None)]
    assert notified(did) is True
    discord_body = next(b for p, _, b, s in platforms.sent if p == "discord" and s == 200)
    assert discord_body["allowed_mentions"] == {"parse": [], "users": [DC_MOD]}
    with db() as conn:
        rows = conn.execute("SELECT 'telegram', short_id, recipient_id, moderator_id::text, action_type FROM "
                            "public.telegram_callback_mapping WHERE donation_id = %s UNION ALL SELECT 'discord', "
                            "short_id, recipient_id, moderator_id::text, action_type FROM "
                            "public.discord_callback_mapping WHERE donation_id = %s", (did, did)).fetchall()
    tg_rows = [r for r in rows if r[0] == "telegram"]
    assert {r[4] for r in tg_rows} == {"approve", "reject", "hide_message"}  # this moderator cannot ban
    assert all(len(r[1]) == 16 and r[2] == TG_MOD and r[3] == tg_mod for r in tg_rows)
    assert {r[2] for r in rows if r[0] == "discord"} == {DC_CHANNEL}


def test_allowlist_and_disabled(clean):
    sid = creator("authtest-p11-allow", moderation_mode="manual")
    add_moderator(sid, telegram=TG_MOD)
    add_moderator(sid, telegram=TG_OTHER)
    did = pending_payment(sid)
    pay(did)
    platforms = Platforms()
    work(platforms.handler(recipients=frozenset({TG_MOD})), did)
    assert [b["chat_id"] for _, _, b, _ in platforms.sent] == [int(TG_MOD)]
    assert ("telegram", "dm", TG_OTHER, "skipped", "not_allowlisted") in deliveries(did)
    off = pending_payment(sid)
    pay(off)
    platforms = Platforms()
    work(platforms.handler(enabled=False), off)
    assert platforms.sent == [] and deliveries(off) == [] and notified(off) is True


# ── webhooks through the real app ───────────────────────────────────────────

def keypair():
    private = Ed25519PrivateKey.generate()
    return private, private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


PRIVATE, PUBLIC = keypair()
# Outside production the bots only act for these creators (HYPERCHAT_BOT_CREATOR_ALLOWLIST).
MANY = [f"authtest-p11-many-{i:02d}" for i in range(1, 13)]  # 11 active assignments + 1 inactive (12)
ALLOWED_CREATORS = ("authtest-p11-tg", "authtest-p11-view", "authtest-p11-dc", "authtest-p11-log",
                    "authtest-p11-repeat", "authtest-p11-repeat-dc", "authtest-p11-not-mine", *MANY)


@pytest.fixture
def bots_app(env, monkeypatch):
    for key, value in {"HYPERCHAT_BOTS_ENABLED": "true", "HYPERCHAT_TELEGRAM_BOT_TOKEN": TOKEN,
                       "HYPERCHAT_TELEGRAM_WEBHOOK_SECRET": SECRET, "HYPERCHAT_DISCORD_BOT_TOKEN": DTOKEN,
                       "HYPERCHAT_DISCORD_PUBLIC_KEY": PUBLIC,
                       "HYPERCHAT_BOT_RECIPIENT_ALLOWLIST": f"{TG_MOD},{TG_OTHER}",
                       "HYPERCHAT_BOT_CREATOR_ALLOWLIST": ",".join(ALLOWED_CREATORS)}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    from app.main import create_app
    application = create_app()
    capture = LogCapture()
    logging.getLogger().addHandler(capture)
    with TestClient(application, base_url="https://testserver") as client:
        application.state.auth.sender = CaptureSender()
        application.state.auth.google = FakeGoogle()
        client.platforms = Platforms()
        application.state.telegram_bot.client = TelegramClient(TOKEN, client.platforms.transport("telegram"))
        client.capture = capture
        yield client
    logging.getLogger().removeHandler(capture)
    with db() as conn:
        conn.execute("DELETE FROM public.rate_limits WHERE endpoint LIKE 'bot:%'")


def tg_post(client, update, secret=SECRET):
    return client.post("/api/v1/webhooks/telegram", content=json.dumps(update),
                       headers={"x-telegram-bot-api-secret-token": secret, "content-type": "application/json"})


def callback(client, platform, sid, did, action, recipient, moderator_id):
    return run(BotRepository(factory()).create_callback(platform, donation_id=did, streamer_id=sid, action=action,
                                                       recipient_id=recipient, moderator_id=moderator_id))


def press(update_id, user, data):
    return {"update_id": update_id, "callback_query": {"id": "q" + str(update_id), "from": {"id": int(user)},
                                                       "data": data, "message": {"message_id": 9, "text": "card",
                                                                                 "chat": {"id": int(user), "type": "private"}}}}


def test_telegram_webhook_end_to_end(bots_app):
    sid = creator("authtest-p11-tg", moderation_mode="manual")
    mod = add_moderator(sid, telegram=TG_MOD)
    add_moderator(sid, telegram=TG_OTHER)
    did = make_donation(sid, 100, payment="success", moderation="pending", message=MESSAGE, name="Ravi")
    data = callback(bots_app, "telegram", sid, did, "approve", TG_MOD, mod)
    assert tg_post(bots_app, press(1, TG_MOD, data), secret="wrong").status_code == 401
    other = tg_post(bots_app, press(600000000 + secrets.randbelow(10**8), TG_OTHER, data))
    assert other.json()["text"].startswith("❌ This button was not sent")
    base = 900000000 + secrets.randbelow(10**8)
    r = tg_post(bots_app, press(base, TG_MOD, data))
    assert r.status_code == 200 and r.json()["method"] == "answerCallbackQuery" and "Approved by Mod One" in r.json()["text"]
    assert r.headers["cache-control"] == "no-store"
    assert tg_post(bots_app, press(base, TG_MOD, data)).content == b""  # the same update again: ignored
    with db() as conn:
        assert conn.execute("SELECT moderation_status FROM public.donations WHERE id = %s", (did,)).fetchone()[0] == "approved"
        assert conn.execute("SELECT action_source, moderator_id::text, moderator_telegram_id, actor_user_id FROM "
                            "public.moderation_actions WHERE donation_id = %s", (did,)).fetchall() == \
            [("telegram", mod, TG_MOD, None)]
        assert conn.execute("SELECT total_actions, last_action_at IS NOT NULL FROM public.streamers_moderators "
                            "WHERE id = %s", (mod,)).fetchone() == (1, True)
        assert conn.execute("SELECT count(*) FROM public.jobs WHERE dedupe_key = %s AND kind = 'donation_audio'",
                            (did,)).fetchone()[0] == 1
        assert conn.execute("SELECT event FROM public.realtime_outbox WHERE subject_id = %s", (did,)).fetchone()[0] == \
            "donation-updated"
    edits = [b for _, path, b, _ in bots_app.platforms.sent if path.endswith("/editMessageText")]
    assert len(edits) == 1 and "Approved by Mod One" in edits[0]["text"]


def test_creator_outside_the_allowlist_gets_no_buttons_and_cannot_be_moderated(bots_app):
    listed = creator("authtest-p11-tg", moderation_mode="manual")
    unlisted = creator("authtest-p11-unlisted", moderation_mode="manual")
    update_streamer(unlisted, streamer_name="Unlisted Creator")
    add_moderator(listed, telegram=TG_MOD)
    unlisted_mod = add_moderator(unlisted, telegram=TG_MOD)
    make_donation(listed, 100, payment="success", moderation="pending", message=MESSAGE, name="Ravi")
    did = make_donation(unlisted, 100, payment="success", moderation="pending", message=MESSAGE, name="Ravi")
    # a button minted for the unlisted creator (e.g. before the allowlist existed) can no longer moderate
    r = tg_post(bots_app, press(700000000 + secrets.randbelow(10**8), TG_MOD,
                                callback(bots_app, "telegram", unlisted, did, "approve", TG_MOD, unlisted_mod)))
    assert "Invalid or expired" in r.json()["text"]
    with db() as conn:
        assert conn.execute("SELECT moderation_status FROM public.donations WHERE id = %s", (did,)).fetchone()[0] == "pending"
        assert conn.execute("SELECT count(*) FROM public.moderation_actions WHERE donation_id = %s",
                            (did,)).fetchone()[0] == 0
        conn.execute("DELETE FROM public.telegram_callback_mapping WHERE streamer_id = %s", (unlisted,))
    for n, text in enumerate(("/pending", "/recent", "/stats", "/status", "/settings")):
        bots_app.platforms.sent.clear()
        tg_post(bots_app, {"update_id": 500000000 + n * 1000 + secrets.randbelow(1000), "message": {
            "message_id": 1, "text": text, "from": {"id": int(TG_MOD)}, "chat": {"id": int(TG_MOD), "type": "private"}}})
        out = "\n".join(str(b.get("text", "")) for _, path, b, _ in bots_app.platforms.sent)
        assert "Unlisted Creator" not in out and bots_app.platforms.sent, text
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.telegram_callback_mapping WHERE streamer_id = %s",
                            (unlisted,)).fetchone()[0] == 0  # /pending minted no button for the unlisted creator
        assert conn.execute("SELECT count(*) FROM public.telegram_callback_mapping WHERE streamer_id = %s",
                            (listed,)).fetchone()[0] > 0


def command_update(text, n):
    return {"update_id": 400000000 + n * 1000 + secrets.randbelow(1000), "message": {
        "message_id": 1, "text": text, "from": {"id": int(TG_MOD)}, "chat": {"id": int(TG_MOD), "type": "private"}}}


def test_commands_cover_every_creator_beyond_ten(bots_app):
    """The old lookup ended in LIMIT 10: a moderator of 11+ creators never saw the rest. Name order is slug order."""
    for i, slug in enumerate(MANY, 1):
        sid = creator(slug, moderation_mode="manual")
        update_streamer(sid, streamer_name=f"P11 Many {i:02d}")
        add_moderator(sid, telegram=TG_MOD, is_active=i <= 11)
        make_donation(sid, 100, payment="success", moderation="pending", message=MESSAGE, name="Ravi")
    other = creator("authtest-p11-not-mine", moderation_mode="manual")  # allow-listed, but not this user's
    update_streamer(other, streamer_name="P11 Many 00 Not Mine")
    add_moderator(other, telegram=TG_OTHER)
    make_donation(other, 100, payment="success", moderation="pending", name="Ravi")
    repo = BotRepository(factory())
    assert len(run(repo.telegram_assignments(TG_MOD))) == 11
    assert len(run(repo.telegram_assignments(TG_MOD, active_only=False))) == 12
    scoped = run(repo.telegram_assignments(TG_MOD, slugs=frozenset({MANY[10]})))
    assert [r["streamer_slug"] for r in scoped] == [MANY[10]]
    for n, (text, template) in enumerate((("/pending", "<b>{} - 1 Pending</b>"),
                                          ("/recent", "No recent donations for {}."), ("/stats", "Stats - {}</b>"),
                                          ("/settings", "Streamer: <b>{}</b>"), ("/status", "🎮 <b>{}</b>"))):
        bots_app.platforms.sent.clear()
        assert tg_post(bots_app, command_update(text, n)).status_code == 200
        out = "\n".join(str(b.get("text", "")) for _, path, b, _ in bots_app.platforms.sent)
        shown = {i for i in range(1, 13) if template.format(f"P11 Many {i:02d}") in out}
        assert shown == (set(range(1, 13)) if text == "/status" else set(range(1, 12))), (text, shown)  # incl. 11th
        assert "Not Mine" not in out and "more than" not in out, text
        if text == "/status":
            assert out.count("❌ Inactive") == 1
    with db() as conn:  # /pending minted buttons for all 11 active assignments, none for the other user's creator
        assert conn.execute("SELECT count(DISTINCT m.streamer_id) FROM public.telegram_callback_mapping m JOIN "
                            "public.streamers s ON s.id = m.streamer_id WHERE s.streamer_slug LIKE "
                            "'authtest-p11-many-%%'").fetchone()[0] == 11
        assert conn.execute("SELECT count(*) FROM public.telegram_callback_mapping WHERE streamer_id = %s",
                            (other,)).fetchone()[0] == 0


def audit(did):
    with db() as conn:
        return conn.execute("SELECT action_type, action_source, previous_status, new_status FROM "
                            "public.moderation_actions WHERE donation_id = %s ORDER BY created_at, id", (did,)).fetchall()


def state_and_total(did, mod):
    with db() as conn:
        return (conn.execute("SELECT moderation_status FROM public.donations WHERE id = %s", (did,)).fetchone()[0],
                conn.execute("SELECT total_actions FROM public.streamers_moderators WHERE id = %s",
                             (mod,)).fetchone()[0])


REVERSAL = [("reject", "telegram", "pending", "rejected"), ("approve", "telegram", "rejected", "approved"),
            ("reject", "telegram", "approved", "rejected")]


def test_telegram_repeated_decision_after_reversal_is_audited_and_duplicates_are_not(bots_app):
    sid = creator("authtest-p11-repeat", moderation_mode="manual")
    mod = add_moderator(sid, telegram=TG_MOD)
    did = make_donation(sid, 100, payment="success", moderation="pending", message=MESSAGE, name="Ravi")
    reject = callback(bots_app, "telegram", sid, did, "reject", TG_MOD, mod)
    approve = callback(bots_app, "telegram", sid, did, "approve", TG_MOD, mod)
    base = 300000000 + secrets.randbelow(10**7) * 10
    answers = [tg_post(bots_app, press(base + i, TG_MOD, data)).json()["text"]
               for i, data in enumerate((reject, approve, reject))]  # the same Reject button, pressed again later
    assert [a.split(" by ")[0] for a in answers] == ["❌ Rejected", "✅ Approved", "❌ Rejected"]
    assert audit(did) == REVERSAL and state_and_total(did, mod) == ("rejected", 3)
    # a redelivered update (same update_id) is dropped before anything runs
    assert tg_post(bots_app, press(base + 2, TG_MOD, reject)).content == b""
    # a new press of the same action on a donation already in that state changes nothing: no audit row, no counter
    assert "No change" in tg_post(bots_app, press(base + 3, TG_MOD, reject)).json()["text"]
    assert audit(did) == REVERSAL and state_and_total(did, mod) == ("rejected", 3)
    with db() as conn:  # the approval in between queued its audio job (Phase 8), once
        assert conn.execute("SELECT count(*) FROM public.jobs WHERE dedupe_key = %s AND kind = 'donation_audio'",
                            (str(did),)).fetchone()[0] == 1


def test_discord_repeated_decision_after_reversal_is_audited_and_duplicates_are_not(bots_app):
    sid = creator("authtest-p11-repeat-dc", moderation_mode="manual", discord_moderation_enabled=True,
                  discord_moderation_channel_id=DC_CHANNEL)
    mod = add_moderator(sid, discord=DC_MOD)
    did = make_donation(sid, 100, payment="success", moderation="pending")
    reject = callback(bots_app, "discord", sid, did, "reject", DC_CHANNEL, None)
    approve = callback(bots_app, "discord", sid, did, "approve", DC_CHANNEL, None)

    def click(custom_id, iid):
        return discord_post(bots_app, {"type": 3, "id": iid, "channel_id": DC_CHANNEL,
                                       "member": {"user": {"id": DC_MOD}}, "data": {"custom_id": custom_id}}).json()
    base = 5 * 10**14 + secrets.randbelow(10**13) * 10
    replies = [click(data, str(base + i)) for i, data in enumerate((reject, approve, reject))]
    assert [r["type"] for r in replies] == [7, 7, 7]
    assert audit(did) == [(a, "discord", p, n) for a, _, p, n in REVERSAL]
    assert state_and_total(did, mod) == ("rejected", 3)
    duplicate = click(reject, str(base + 2))  # the same interaction delivered again
    assert duplicate["type"] == 4 and "Already handled" in duplicate["data"]["content"]
    again = click(reject, str(base + 3))  # a new click of the same action: unchanged, ephemeral
    assert again["type"] == 4 and "No change" in again["data"]["content"]
    assert len(audit(did)) == 3 and state_and_total(did, mod) == ("rejected", 3)


def test_telegram_viewer_and_disabled(bots_app):
    sid = creator("authtest-p11-view", moderation_mode="manual")
    viewer = add_moderator(sid, telegram=TG_MOD, role="viewer")
    did = make_donation(sid, 100, payment="success", moderation="pending")
    r = tg_post(bots_app, press(800000000 + secrets.randbelow(10**8), TG_MOD,
                                callback(bots_app, "telegram", sid, did, "approve", TG_MOD, viewer)))
    assert "No permission" in r.json()["text"]
    update_streamer(sid, telegram_moderation_enabled=False)
    with db() as conn:
        conn.execute("UPDATE public.streamers_moderators SET role = 'owner' WHERE id = %s", (viewer,))
    r = tg_post(bots_app, press(800000000 + secrets.randbelow(10**8), TG_MOD,
                                callback(bots_app, "telegram", sid, did, "approve", TG_MOD, viewer)))
    assert "disabled" in r.json()["text"]
    with db() as conn:
        assert conn.execute("SELECT moderation_status FROM public.donations WHERE id = %s", (did,)).fetchone()[0] == "pending"


def discord_post(client, body: dict, private=PRIVATE, ts=None):
    raw = json.dumps(body).encode()
    ts = str(ts or int(time.time()))
    return client.post("/api/v1/webhooks/discord", content=raw, headers={
        "x-signature-ed25519": private.sign(ts.encode() + raw).hex(), "x-signature-timestamp": ts,
        "content-type": "application/json"})


def test_discord_webhook_end_to_end(bots_app):
    sid = creator("authtest-p11-dc", moderation_mode="manual", discord_moderation_enabled=True,
                  discord_moderation_channel_id=DC_CHANNEL)
    add_moderator(sid, discord=DC_MOD, can_ban=False)
    did = make_donation(sid, 100, payment="success", moderation="pending")
    assert discord_post(bots_app, {"type": 1}).json() == {"type": 1}
    other, _ = keypair()
    assert discord_post(bots_app, {"type": 1}, private=other).status_code == 401
    ban = callback(bots_app, "discord", sid, did, "ban_donor", DC_CHANNEL, None)
    approve = callback(bots_app, "discord", sid, did, "approve", DC_CHANNEL, None)

    def click(custom_id, iid):
        return discord_post(bots_app, {"type": 3, "id": iid, "channel_id": DC_CHANNEL,
                                       "member": {"user": {"id": DC_MOD}}, "data": {"custom_id": custom_id}}).json()
    refused = click(ban, str(secrets.randbelow(10**15)))
    assert refused["type"] == 4 and refused["data"]["flags"] == 64 and "permission" in refused["data"]["content"]
    ok = click(approve, str(secrets.randbelow(10**15)))
    assert ok["type"] == 7 and "Approved by" in ok["data"]["content"]
    with db() as conn:  # the widened CHECK accepts the Discord audit row
        assert conn.execute("SELECT action_source, moderator_telegram_id FROM public.moderation_actions "
                            "WHERE donation_id = %s", (did,)).fetchall() == [("discord", DC_MOD)]


def test_disabled_by_default(app):
    assert app.post("/api/v1/webhooks/telegram", content=b"{}").status_code == 503
    assert app.post("/api/v1/webhooks/discord", content=b"{}").status_code == 503


def test_nothing_sensitive_in_logs(bots_app):
    sid = creator("authtest-p11-log", moderation_mode="manual")
    mod = add_moderator(sid, telegram=TG_MOD)
    did = make_donation(sid, 100, payment="success", moderation="pending", message=MESSAGE, name="Secret Donor Name")
    data = callback(bots_app, "telegram", sid, did, "approve", TG_MOD, mod)
    tg_post(bots_app, press(700000000 + secrets.randbelow(10**8), TG_MOD, data))
    tg_post(bots_app, {"update_id": 700000000 + secrets.randbelow(10**8), "message": {
        "text": "/pending", "from": {"id": int(TG_MOD)}, "chat": {"id": int(TG_MOD), "type": "private"}}})
    tg_post(bots_app, press(1, TG_MOD, data), secret="wrong")
    text = "\n".join(bots_app.capture.records)
    for value in (TOKEN, SECRET, DTOKEN, MESSAGE, "Secret Donor Name", TG_MOD, "callback_query", data):
        assert value not in text
    assert "telegram callback" in text and "bot_webhook_rejected" in text
