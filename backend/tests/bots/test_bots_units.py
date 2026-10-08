"""Phase 11 units (no database, no real Telegram/Discord): escaping, message building, permissions, API error
classification (mocked transports), Discord signatures, the notification job's decisions, and both webhooks'
authentication, deduplication, callback authorization and logging."""

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.bots import format as fmt
from app.bots.clients import DiscordClient, TelegramClient, verify_discord
from app.bots.discord import DiscordBot
from app.bots.notify import ModeratorNotifyHandler
from app.bots.permissions import can
from app.bots.repository import Target, new_callback_id
from app.bots.telegram import TelegramBot
from app.dashboard.moderation import NotPaid, Step
from app.jobs.queue import Job
from app.jobs.retry import Permanent, Retryable
from app.security.nonce_store import NonceReplayError

BASE, FRONT = "https://media.example.test", "https://app.example.test"
SID, DID = str(uuid.uuid4()), str(uuid.uuid4())
TOKEN, SECRET = "123456:TEST-ONLY-bot-token-abcdef", "TEST-ONLY-webhook-secret-0123456789"
HOSTILE_NAME = "<b>x</b> & @everyone **bold** `code` <@1234567890123456>"
MESSAGE = "a private donor message </i><script>alert(1)</script> @here"


def run(coro):
    return asyncio.run(coro)


def donation(**kw):
    d = dict(id=DID, streamer_id=SID, streamer_slug="ankit", streamer_name="Ankit", donor_name=HOSTILE_NAME,
             amount=Decimal("150.00"), currency="INR", message=MESSAGE, message_visible=True, payment_status="success",
             moderation_status="pending", voice_message_url=None, media_url=None, media_type=None,
             created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC), mod_notified=False, too_old=False,
             telegram_enabled=True, discord_enabled=True, discord_moderation_channel_id=None,
             moderation_mode="manual")
    d.update(kw)
    return d


def moderator(**kw):
    m = dict(id=str(uuid.uuid4()), streamer_id=SID, mod_name="Mod <i>", telegram_user_id="111111111",
             discord_user_id="222222222222222222", role="moderator", can_approve=True, can_reject=True,
             can_hide_message=True, can_ban=False, is_active=True, total_actions=7, telegram_enabled=True,
             discord_enabled=True, moderation_mode="manual", streamer_name="Ankit", streamer_slug="ankit",
             created_at=datetime(2026, 1, 1, tzinfo=UTC), discord_moderation_channel_id=None)
    m.update(kw)
    return m


# ── escaping and messages (D8) ──────────────────────────────────────────────

def test_telegram_text_escapes_all_donor_text():
    text = fmt.telegram_text(donation())
    assert "<b>x</b>" not in text and "&lt;b&gt;x&lt;/b&gt; &amp; @everyone" in text
    assert "<script>" not in text and "&lt;script&gt;" in text and "₹150 INR" in text
    hidden = fmt.telegram_text(donation(message_visible=False))
    assert "script" not in hidden


def test_message_excerpt():
    long = donation(message="x" * 300)
    assert "x" * 200 + "..." in fmt.telegram_text(long) and "x" * 201 not in fmt.telegram_text(long)


def test_discord_payload_escapes_and_limits_mentions():
    mods = ["333333333333333333", "444444444444444444"]
    p = fmt.discord_payload(donation(), {"approve": "m:" + "a" * 16}, [("📊 Dashboard", f"{FRONT}/dashboard/ankit")], mods)
    assert p["allowed_mentions"] == {"parse": [], "users": mods}  # donor text can never ping anyone
    assert p["content"].startswith("<@333333333333333333> <@444444444444444444>")
    raw = json.dumps(p)
    assert "**bold**" not in raw and "\\\\*\\\\*bold\\\\*\\\\*" in raw and "\\\\<\\\\@1234567890123456\\\\>" in raw
    assert p["components"][0]["components"][0]["custom_id"] == "m:" + "a" * 16
    approved = fmt.discord_payload(donation(moderation_status="auto_approved"), {}, [], mods)
    assert "content" not in approved and approved["allowed_mentions"]["users"] == []


def test_links_only_this_creators_uploads():
    good = f"{BASE}/voice-messages/ankit_1800000000000_{'a' * 32}.webm"
    bad = f"{BASE}/voice-messages/other_1800000000000_{'a' * 32}.webm"
    assert fmt.links(donation(voice_message_url=good), BASE, FRONT)[0] == ("🎵 Listen Voice", good)
    assert fmt.links(donation(voice_message_url=bad), BASE, FRONT) == [("📊 Dashboard", f"{FRONT}/dashboard/ankit")]
    assert fmt.links(donation(media_url="https://evil.example/x.png"), BASE, FRONT)[0][0] == "📊 Dashboard"


def test_keyboard_layout():
    kb = fmt.telegram_keyboard({"approve": "m:a", "ban_donor": "m:b"}, [("📊 Dashboard", "https://x/y")])
    assert kb == [[{"text": "✅ Approve", "callback_data": "m:a"}], [{"text": "🚫 Ban", "callback_data": "m:b"}],
                  [{"text": "📊 Dashboard", "url": "https://x/y"}]]


@pytest.mark.parametrize("mod, allowed", [
    ({"role": "owner", "can_approve": False, "can_ban": False}, {"approve", "reject", "hide_message", "ban_donor"}),
    ({"role": "viewer"}, set()),
    ({}, {"approve", "reject", "hide_message"}),
    ({"can_ban": True, "can_reject": False}, {"approve", "hide_message", "ban_donor"}),
    ({"can_approve": None, "can_ban": None}, {"approve", "reject", "hide_message"}),  # column defaults
    ({"is_active": False, "role": "owner"}, set()),
])
def test_permissions(mod, allowed):
    m = moderator(**mod)
    assert {a for a in ("approve", "reject", "hide_message", "ban_donor", "replay") if can(m, a)} == allowed


def test_callback_ids():
    ids = {new_callback_id() for _ in range(200)}
    assert len(ids) == 200 and all(len(i) == 16 and i.isalnum() for i in ids)


# ── clients (mocked transports) ─────────────────────────────────────────────

def telegram(handler):
    return TelegramClient(TOKEN, httpx.MockTransport(handler))


@pytest.mark.parametrize("status, body, kind, reason, retry_after", [
    (429, {"ok": False, "parameters": {"retry_after": 7}}, Retryable, "telegram_rate_limited", 7.0),
    (502, {}, Retryable, "telegram_server_error", None),
    (400, {"ok": False, "description": "Bad Request: can't parse " + MESSAGE}, Permanent, "telegram_rejected", None),
    (401, {}, Permanent, "telegram_unauthorized", None), (403, {}, Permanent, "telegram_forbidden", None),
    (404, {}, Permanent, "telegram_not_found", None),
])
def test_telegram_errors(status, body, kind, reason, retry_after):
    with pytest.raises(kind) as exc:
        run(telegram(lambda r: httpx.Response(status, json=body)).send_message("1", "hi"))
    assert exc.value.reason == reason and TOKEN not in str(exc.value) and MESSAGE not in str(exc.value)
    if kind is Retryable:
        assert exc.value.retry_after == retry_after


def test_telegram_send_and_network_errors():
    seen = {}

    def ok(request):
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
    assert run(telegram(ok).send_message("123", "<b>x</b>", [[{"text": "a", "callback_data": "m:x"}]])) == "42"
    assert seen["url"].endswith("/sendMessage") and seen["body"]["parse_mode"] == "HTML" and seen["body"]["chat_id"] == 123
    for error, reason in ((httpx.ReadTimeout("t"), "telegram_timeout"), (httpx.ConnectError("c"), "telegram_network")):
        def boom(request, e=error):
            raise e
        with pytest.raises(Retryable) as exc:
            run(telegram(boom).send_message("1", "x"))
        assert exc.value.reason == reason and TOKEN not in str(exc.value)
    assert TOKEN not in repr(telegram(ok))


def test_discord_client():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.headers["authorization"]))
        if request.url.path.endswith("/users/@me/channels"):
            return httpx.Response(200, json={"id": "999999999999999999"})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"id": "888"})
        return httpx.Response(429, json={"retry_after": 1.5})
    c = DiscordClient(TOKEN, httpx.MockTransport(handler))
    assert run(c.open_dm("222222222222222222")) == "999999999999999999"
    assert run(c.create_message("999999999999999999", {"content": "x"})) == "888"
    with pytest.raises(Retryable) as exc:
        run(c.get_application())
    assert exc.value.retry_after == 1.5 and calls[0][2] == f"Bot {TOKEN}"


# ── Discord signatures (D3) ─────────────────────────────────────────────────

def keypair():
    private = Ed25519PrivateKey.generate()
    return private, private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def signed(private, body: bytes, ts: int) -> tuple[str, str]:
    return private.sign(str(ts).encode() + body).hex(), str(ts)


def test_discord_signature():
    private, public = keypair()
    body, now = b'{"type":1}', int(time.time())
    sig, ts = signed(private, body, now)
    assert verify_discord(public, sig, ts, body, now)
    assert not verify_discord(public, sig, ts, body + b" ", now)                     # tampered
    assert not verify_discord(public, sig, ts, body, now + 301)                      # replayed later (> 5 min)
    other, _ = keypair()
    assert not verify_discord(public, signed(other, body, now)[0], ts, body, now)    # another key
    assert not verify_discord(public, "zz", ts, body, now) and not verify_discord(public, None, ts, body, now)
    assert not verify_discord(public, sig, "not-a-number", body, now)


# ── the notification job ────────────────────────────────────────────────────

class FakeRepo:
    def __init__(self, d=None, mods=()):
        self.d, self.mods = d, list(mods)
        self.rows, self.callbacks, self.notified, self.gave_up = {}, [], [], []

    async def notify_context(self, did):
        return self.d

    async def moderators(self, sid):
        return self.mods

    async def ensure_deliveries(self, did, sid, targets):
        for t in targets:
            key = (t.platform, t.target_kind, t.target_id)
            self.rows.setdefault(key, {"id": str(uuid.uuid4()), "moderator_id": t.moderator_id, "platform": t.platform,
                                       "target_kind": t.target_kind, "target_id": t.target_id, "status": "pending"})
        return list(self.rows.values())

    async def mark_delivery(self, delivery_id, status, *, reason=None, message_id=None):
        for row in self.rows.values():
            if row["id"] == delivery_id:
                row.update(status=status, reason=reason)

    async def create_callback(self, platform, **kw):
        self.callbacks.append((platform, kw))
        return "m:" + new_callback_id()

    async def mark_notified(self, cur, did):
        self.notified.append(did)

    async def give_up(self, cur, did, reason):
        self.gave_up.append((did, reason))


def job():
    return Job(str(uuid.uuid4()), "moderator_notify", DID, {"donation_id": DID}, 1, 5, "w1", False)


def handler(repo, *, tg=None, dc=None, enabled=True, recipients=None):
    return ModeratorNotifyHandler(repo, telegram=tg, discord=dc, enabled=enabled, recipients=recipients,
                                  frontend_url=FRONT, public_base=BASE)


def sends(results):
    """A Telegram mock answering each send with the next result (status code)."""
    sent = []

    def h(request):
        body = json.loads(request.content)
        sent.append(body)
        status = results.pop(0) if results else 200
        return httpx.Response(status, json={"ok": status == 200, "result": {"message_id": len(sent)}})
    return TelegramClient(TOKEN, httpx.MockTransport(h)), sent


def finish(done, repo):
    if done.effects:
        run(done.effects(None))
    return done.result


@pytest.mark.parametrize("d, reason", [
    (donation(mod_notified=True), "already_notified"), (donation(payment_status="pending"), "not_eligible"),
    (donation(moderation_status="rejected"), "not_eligible"), (donation(too_old=True), "too_old"),
])
def test_job_skips(d, reason):
    repo = FakeRepo(d, [moderator()])
    tg, sent = sends([])
    assert finish(run(handler(repo, tg=tg).run(job())), repo)["reason"] == reason and sent == []


def test_disabled_bots_send_nothing_but_finish():
    repo = FakeRepo(donation(), [moderator()])
    tg, sent = sends([])
    result = finish(run(handler(repo, tg=tg, enabled=False).run(job())), repo)
    assert result["reason"] == "bots_disabled" and sent == [] and repo.notified == [DID]


def test_telegram_dm_with_permission_buttons():
    m = moderator(can_ban=False)
    repo = FakeRepo(donation(), [m, moderator(telegram_user_id="not-a-number", discord_user_id=None)])
    tg, sent = sends([])
    result = finish(run(handler(repo, tg=tg).run(job())), repo)
    assert result["sent"] == 1 and len(sent) == 1 and repo.notified == [DID]
    buttons = [b.get("callback_data") for row in sent[0]["reply_markup"]["inline_keyboard"] for b in row]
    assert len([b for b in buttons if b]) == 3  # approve, reject, hide (no ban)
    assert all(kw["recipient_id"] == "111111111" and kw["moderator_id"] == m["id"] for _, kw in repo.callbacks)
    assert "<b>x</b>" not in sent[0]["text"]


def test_allowlist_outside_production():
    repo = FakeRepo(donation(), [moderator(), moderator(telegram_user_id="333333333")])
    tg, sent = sends([])
    result = finish(run(handler(repo, tg=tg, recipients=frozenset({"333333333"})).run(job())), repo)
    assert result["sent"] == 1 and result["skipped"] == 1 and [s["chat_id"] for s in sent] == [333333333]
    assert {r["reason"] for r in repo.rows.values() if r["status"] == "skipped"} == {"not_allowlisted"}
    repo2 = FakeRepo(donation(), [moderator()])
    tg2, sent2 = sends([])
    finish(run(handler(repo2, tg=tg2, recipients=frozenset()).run(job())), repo2)
    assert sent2 == []  # an empty allowlist sends nothing at all


def test_transient_failure_retries_only_the_pending_recipient():
    repo = FakeRepo(donation(), [moderator(), moderator(telegram_user_id="333333333")])
    tg, sent = sends([200, 502])
    with pytest.raises(Retryable):
        run(handler(repo, tg=tg).run(job()))
    assert sorted(r["status"] for r in repo.rows.values()) == ["pending", "sent"] and repo.notified == []
    tg2, sent2 = sends([])
    result = finish(run(handler(repo, tg=tg2).run(job())), repo)
    assert len(sent2) == 1 and sent2[0]["chat_id"] == 333333333 and repo.notified == [DID]
    assert result["sent"] == 2  # the earlier delivery (not re-sent) + the retried one


def test_permanent_failure_is_per_recipient():
    repo = FakeRepo(donation(), [moderator(), moderator(telegram_user_id="333333333")])
    tg, _ = sends([403, 200])
    result = finish(run(handler(repo, tg=tg).run(job())), repo)
    assert result["failed"] == 1 and result["sent"] == 1 and repo.notified == [DID]


def test_fallback_gives_up():
    repo = FakeRepo(donation(), [])
    effects = run(handler(repo).fallback(job(), "telegram_server_error"))
    run(effects(None))
    assert repo.gave_up == [(DID, "telegram_server_error")]


def test_discord_channel_or_dms():
    mods = [moderator(), moderator(discord_user_id="555555555555555555", can_ban=True)]
    h = handler(FakeRepo(), dc=object())
    assert h.targets(donation(discord_moderation_channel_id="777777777777777777"), mods) == \
        [Target("discord", "channel", "777777777777777777", None)]
    assert [t.target_id for t in h.targets(donation(), mods)] == ["222222222222222222", "555555555555555555"]
    assert h.targets(donation(discord_enabled=False, telegram_enabled=False), mods) == []


# ── the Telegram webhook ────────────────────────────────────────────────────

class Nonces:
    def __init__(self):
        self.seen = set()

    async def insert(self, nonce, user_id, expires_at):
        if nonce in self.seen:
            raise NonceReplayError(nonce)
        self.seen.add(nonce)


class Limiter:
    def __init__(self, allow=True):
        self.allow, self.hits = allow, []

    async def hit_rate_limit(self, key, bucket, limit, window):
        self.hits.append((key, bucket))
        return self.allow


class Moderation:
    def __init__(self, result=("pending", Step("applied", "approved", True, approve=True), {}), error=None):
        self.result, self.error, self.calls = result, error, []

    async def moderate(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.result


class BotRepo:
    def __init__(self, row=None, mod=None, assignments=()):
        self.row, self.mod, self.assignments, self.callbacks = row, mod, list(assignments), []

    async def callback(self, platform, short_id):
        return self.row

    async def moderator_for(self, platform, sid, user_id):
        return self.mod if self.mod and (self.mod.get("telegram_user_id") == user_id or
                                         self.mod.get("discord_user_id") == user_id) else None

    async def telegram_assignments(self, user_id, active_only=True):
        return self.assignments

    async def pending_count(self, sid):
        return 3

    async def pending_donations(self, sid, limit=5):
        return [donation()]

    async def recent_donations(self, sid, limit=5):
        return [dict(donor_name=HOSTILE_NAME, amount=Decimal("10"), currency="INR", message=MESSAGE,
                     message_visible=False, created_at=datetime.now(UTC))]

    async def create_callback(self, platform, **kw):
        self.callbacks.append(kw)
        return "m:" + new_callback_id()


def tbot(repo, *, moderation=None, limiter=None, recipients=None, sent=None):
    def h(request):
        if sent is not None:
            sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    return TelegramBot(secret=SECRET, client=TelegramClient(TOKEN, httpx.MockTransport(h)), repo=repo,
                       moderation=moderation or Moderation(), nonces=Nonces(), limiter=limiter or Limiter(),
                       recipients=recipients, frontend_url=FRONT, public_base=BASE)


def callback_update(update_id=1, user="111111111", data=None, chat=None):
    return json.dumps({"update_id": update_id, "callback_query": {
        "id": "cbq1", "from": {"id": int(user)}, "data": data or "m:" + "A" * 16,
        "message": {"message_id": 5, "chat": {"id": int(chat or user), "type": "private"}, "text": "card"}}}).encode()


def row(**kw):
    r = dict(short_id="A" * 16, donation_id=DID, streamer_id=SID, action_type="approve", recipient_id="111111111",
             moderator_id=str(uuid.uuid4()), expired=False)
    r.update(kw)
    return r


def test_telegram_secret():
    bot = tbot(BotRepo())
    assert bot.authentic(SECRET) and not bot.authentic(None) and not bot.authentic("") and not bot.authentic(SECRET + "x")


def test_telegram_approve_through_the_shared_core():
    mod = moderator()
    moderation = Moderation()
    status, body = run(tbot(BotRepo(row(), mod), moderation=moderation).handle(callback_update()))
    assert status == 200 and body["method"] == "answerCallbackQuery" and "Approved by Mod <i>" in body["text"]
    (args, kwargs), = moderation.calls
    assert args == (SID, DID, "approve") and kwargs["bot"].platform == "telegram"
    assert kwargs["bot"].moderator_id == mod["id"] and kwargs["bot"].platform_user_id == "111111111"


@pytest.mark.parametrize("repo_row, mod, data, user, expected", [
    (None, moderator(), None, "111111111", "Invalid or expired"),
    (row(expired=True), moderator(), None, "111111111", "expired"),
    (row(), moderator(), "a_ABCDEFGH", "111111111", "Invalid or expired"),            # legacy format refused
    (row(), moderator(), "approve_" + DID + "_ankit_donations", "111111111", "Invalid or expired"),
    (row(recipient_id="999"), moderator(), None, "111111111", "not sent to you"),
    (row(), None, None, "111111111", "not a registered moderator"),
    (row(), moderator(telegram_enabled=False), None, "111111111", "disabled"),
    (row(), moderator(role="viewer"), None, "111111111", "No permission"),
    (row(action_type="ban_donor"), moderator(can_ban=False), None, "111111111", "No permission"),
    (row(action_type="replay"), moderator(role="owner"), None, "111111111", "No permission"),  # no bot replay
])
def test_telegram_refusals(repo_row, mod, data, user, expected):
    moderation = Moderation()
    _, body = run(tbot(BotRepo(repo_row, mod), moderation=moderation).handle(callback_update(user=user, data=data)))
    assert expected in body["text"] and moderation.calls == []


def test_telegram_not_paid_and_unchanged():
    _, body = run(tbot(BotRepo(row(), moderator()), moderation=Moderation(error=NotPaid())).handle(callback_update()))
    assert "not paid" in body["text"]
    unchanged = Moderation(result=("approved", Step("unchanged", "approved", True), {}))
    _, body = run(tbot(BotRepo(row(), moderator()), moderation=unchanged).handle(callback_update(update_id=2)))
    assert "No change" in body["text"]


def test_telegram_duplicate_updates_are_ignored():
    moderation = Moderation()
    bot = tbot(BotRepo(row(), moderator()), moderation=moderation)
    assert run(bot.handle(callback_update(update_id=9)))[1] is not None
    assert run(bot.handle(callback_update(update_id=9))) == (200, None)
    assert len(moderation.calls) == 1


def test_telegram_malformed_bodies():
    bot = tbot(BotRepo())
    assert run(bot.handle(b"not json"))[0] == 400 and run(bot.handle(b'{"x":1}'))[0] == 400
    assert run(bot.handle(b"x" * (256 * 1024 + 1)))[0] == 413


def command(text, user="111111111", chat_type="private", update_id=20):
    return json.dumps({"update_id": update_id, "message": {"message_id": 1, "text": text, "from": {"id": int(user)},
                                                           "chat": {"id": int(user), "type": chat_type}}}).encode()


def test_stats_never_shows_revenue():
    sent = []
    run(tbot(BotRepo(assignments=[moderator()]), sent=sent).handle(command("/stats")))
    (msg,) = sent
    assert "Pending: 3" in msg["text"] and "Your Actions: 7" in msg["text"]
    assert "Revenue" not in msg["text"] and "₹" not in msg["text"]


def test_commands_escape_and_respect_allowlist_rate_limit_and_private_chats():
    sent = []
    run(tbot(BotRepo(assignments=[moderator()]), sent=sent).handle(command("/recent")))
    assert "<b>x</b>" not in sent[0]["text"] and MESSAGE not in sent[0]["text"]  # hidden message not shown
    sent.clear()
    run(tbot(BotRepo(assignments=[moderator()]), sent=sent).handle(command("/pending")))
    assert any("reply_markup" in s for s in sent)
    for bot_kwargs, text, chat_type in (({"recipients": frozenset({"999"})}, "/start", "private"),
                                        ({"limiter": Limiter(allow=False)}, "/start", "private"),
                                        ({}, "/start", "group")):
        sent.clear()
        run(tbot(BotRepo(), sent=sent, **bot_kwargs).handle(command(text, chat_type=chat_type)))
        assert sent == []


def test_telegram_logs_nothing_sensitive(caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.WARNING, logger="httpx")
    bot = tbot(BotRepo(row(), moderator(), assignments=[moderator()]))
    run(bot.handle(callback_update(update_id=30)))
    run(bot.handle(command("/pending", update_id=31)))
    text = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    for value in (TOKEN, SECRET, MESSAGE, HOSTILE_NAME, "111111111", "update_id", "callback_query"):
        assert value not in text


# ── the Discord interactions endpoint ───────────────────────────────────────

def dbot(repo, public, moderation=None):
    return DiscordBot(public_key=public, repo=repo, moderation=moderation or Moderation(), nonces=Nonces())


def interaction(user="222222222222222222", channel="777777777777777777", custom_id=None, iid="1001"):
    return json.dumps({"type": 3, "id": iid, "channel_id": channel, "member": {"user": {"id": user}},
                       "data": {"custom_id": custom_id or "m:" + "B" * 16}}).encode()


def call(bot, private, body, ts=None):
    sig, ts = signed(private, body, ts or int(time.time()))
    return run(bot.handle(body, sig, ts))


def test_discord_ping_and_signature():
    private, public = keypair()
    bot = dbot(BotRepo(), public)
    assert call(bot, private, b'{"type":1}') == (200, {"type": 1})
    assert run(bot.handle(b'{"type":1}', "00" * 64, str(int(time.time()))))[0] == 401
    other, _ = keypair()
    assert call(bot, other, b'{"type":1}')[0] == 401
    assert call(bot, private, b'{"type":1}', ts=int(time.time()) - 600)[0] == 401


def test_discord_channel_button_approve():
    private, public = keypair()
    moderation = Moderation()
    bot = dbot(BotRepo(row(recipient_id="777777777777777777", moderator_id=None), moderator()), public, moderation)
    status, body = call(bot, private, interaction())
    assert status == 200 and body["type"] == 7 and body["data"]["components"] == []
    assert body["data"]["allowed_mentions"] == {"parse": []} and "Approved by" in body["data"]["content"]
    (args, kwargs), = moderation.calls
    assert kwargs["bot"].platform == "discord" and kwargs["bot"].platform_user_id == "222222222222222222"
    assert call(bot, private, interaction())[1]["data"]["flags"] == 64 and len(moderation.calls) == 1  # duplicate id


@pytest.mark.parametrize("repo_row, mod, user, channel, expected", [
    (row(recipient_id="777777777777777777", moderator_id=None), moderator(), "222222222222222222",
     "888888888888888888", "not valid here"),                                         # channel button elsewhere
    (row(recipient_id="555555555555555555"), moderator(), "222222222222222222", "1", "not sent to you"),  # DM button
    (row(recipient_id="777777777777777777", moderator_id=None), None, "222222222222222222", "777777777777777777",
     "not a registered moderator"),
    (row(recipient_id="777777777777777777", moderator_id=None, action_type="ban_donor"), moderator(can_ban=False),
     "222222222222222222", "777777777777777777", "permission"),
    (row(recipient_id="777777777777777777", moderator_id=None), moderator(discord_enabled=False),
     "222222222222222222", "777777777777777777", "disabled"),
    (row(recipient_id="777777777777777777", moderator_id=None, expired=True), moderator(), "222222222222222222",
     "777777777777777777", "expired"),
])
def test_discord_refusals_are_ephemeral_and_never_moderate(repo_row, mod, user, channel, expected):
    private, public = keypair()
    moderation = Moderation()
    status, body = call(dbot(BotRepo(repo_row, mod), public, moderation), private,
                        interaction(user=user, channel=channel, iid=str(int(time.time() * 1000))))
    assert status == 200 and body["type"] == 4 and body["data"]["flags"] == 64 and expected in body["data"]["content"]
    assert moderation.calls == []


def test_discord_unchanged_is_reported_honestly():
    private, public = keypair()
    unchanged = Moderation(result=("approved", Step("unchanged", "approved", True), {}))
    bot = dbot(BotRepo(row(recipient_id="777777777777777777", moderator_id=None), moderator()), public, unchanged)
    _, body = call(bot, private, interaction(iid="2002"))
    assert body["type"] == 4 and "No change" in body["data"]["content"]
