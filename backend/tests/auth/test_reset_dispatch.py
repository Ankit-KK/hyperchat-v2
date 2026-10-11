"""Security audit F11 follow-up: bounded reset-email work after the forgot-password response.

AuthService.send_reset_link runs at most RESET_EMAIL_SLOTS at once per process and sheds the rest (no queue), and each
run ends within RESET_EMAIL_DEADLINE_SECONDS. No database: an in-memory repository and gated senders, so every
interleaving is forced with events (the deadline test uses a sender that never finishes). Synthetic data only.
"""

import asyncio
import logging

from app.auth import service as svc_mod
from app.auth.repository import User
from app.auth.service import AuthService
from app.core.config import Settings

DOMAIN = "@dispatch.invalid"


class MemoryRepo:
    def __init__(self) -> None:
        self.lookups, self.tokens = [], []

    async def user_by_email(self, email: str) -> User | None:
        self.lookups.append(email)
        return User(id=f"id-{email.split('@')[0]}", email=email, username=None, role="streamer", streamer_id=None,
                    is_active=True, password_hash=None, failed_login_attempts=0, locked_until=None)

    async def create_reset_token(self, user_id: str, token_hash: str, ttl: int) -> None:
        self.tokens.append(user_id)


class GatedSender:
    """send() waits until released; `started` counts sends in flight."""

    def __init__(self) -> None:
        self.started, self.sent, self.release = 0, [], asyncio.Event()

    async def send(self, email) -> None:
        self.started += 1
        await self.release.wait()
        self.sent.append(email)


class FailingSender:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, email) -> None:
        self.calls += 1
        raise ConnectionError("provider unavailable")


def make_service(sender, slots: int = 2) -> AuthService:
    s = AuthService(Settings(db_enabled=False, frontend_url="https://hyperchat.invalid"), MemoryRepo(), sender, None)
    s.reset_slots = asyncio.Semaphore(slots)
    return s


async def until(predicate) -> None:
    for _ in range(1000):  # bounded spin over event-loop turns, not a timing assumption
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never reached")


def events(caplog, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "security_event", None) == name]


def test_saturated_slots_shed_extra_requests_without_lookup_token_or_send(caplog):
    caplog.set_level(logging.INFO, logger="hyperchat.security")
    sender = GatedSender()
    s = make_service(sender, slots=2)

    async def scenario():
        busy = [asyncio.create_task(s.send_reset_link(f"busy{i}" + DOMAIN, "203.0.113.1")) for i in range(2)]
        await until(lambda: sender.started == 2)  # both slots are inside the provider call
        await s.send_reset_link("shed1" + DOMAIN, "203.0.113.2")  # returns at once: shed
        await s.send_reset_link("shed2" + DOMAIN, "203.0.113.3")
        assert s.repo.lookups == ["busy0" + DOMAIN, "busy1" + DOMAIN] and len(s.repo.tokens) == 2
        sender.release.set()
        await asyncio.gather(*busy)
        await s.send_reset_link("after" + DOMAIN, "203.0.113.4")  # slots are free again
    asyncio.run(scenario())
    assert [e.to for e in sender.sent] == ["busy0" + DOMAIN, "busy1" + DOMAIN, "after" + DOMAIN]
    assert len(events(caplog, "password_reset_email_shed")) == 2
    assert len(events(caplog, "password_reset_requested")) == 3


def test_concurrency_never_exceeds_the_slots_under_a_burst():
    sender = GatedSender()
    s = make_service(sender, slots=3)

    async def scenario():
        tasks = [asyncio.create_task(s.send_reset_link(f"burst{i}" + DOMAIN, "203.0.113.5")) for i in range(50)]
        await until(lambda: sender.started == 3)
        await until(lambda: all(t.done() for t in tasks[3:]))  # everything beyond the slots was shed, not queued
        in_flight = sender.started
        sender.release.set()
        await asyncio.gather(*tasks)
        return in_flight
    assert asyncio.run(scenario()) == 3
    assert len(sender.sent) == 3


def test_a_provider_failure_frees_its_slot_and_logs_no_address_token_or_link(caplog):
    caplog.set_level(logging.INFO, logger="hyperchat.security")
    sender = FailingSender()
    s = make_service(sender, slots=1)

    async def scenario():
        for i in range(3):  # more failures than slots: each one must give its slot back
            await s.send_reset_link(f"fail{i}" + DOMAIN, "203.0.113.6")
    asyncio.run(scenario())
    assert sender.calls == 3 and not s.reset_slots.locked()
    failed = events(caplog, "password_reset_email_failed")
    assert [r.reason for r in failed] == ["ConnectionError"] * 3
    text = " ".join(str(r.__dict__) for r in caplog.records)
    assert DOMAIN not in text and "reset-password?token=" not in text and "provider unavailable" not in text


def test_a_provider_that_never_answers_is_cut_off_at_the_deadline(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="hyperchat.security")
    monkeypatch.setattr(svc_mod, "RESET_EMAIL_DEADLINE_SECONDS", 0.05)
    sender = GatedSender()  # never released: the send would hang forever
    s = make_service(sender, slots=1)

    async def scenario():
        await asyncio.wait_for(s.send_reset_link("hang" + DOMAIN, "203.0.113.7"), 5)  # outer guard only
        assert not s.reset_slots.locked()  # the slot is free again
    asyncio.run(scenario())
    assert sender.started == 1 and sender.sent == []
    assert [r.reason for r in events(caplog, "password_reset_email_failed")] == ["TimeoutError"]


def test_each_process_has_its_own_cap_so_the_deployment_bound_is_workers_times_slots():
    """Two AuthService instances stand for the two uvicorn worker processes: each enforces its own slots and nothing
    is shared, so the bound for the deployment is workers x slots (pinned in tests/test_deploy_production.py)."""
    senders = [GatedSender(), GatedSender()]
    workers = [make_service(sender, slots=2) for sender in senders]

    async def scenario():
        tasks = [asyncio.create_task(w.send_reset_link(f"w{n}r{i}" + DOMAIN, "203.0.113.8"))
                 for n, w in enumerate(workers) for i in range(10)]
        await until(lambda: [x.started for x in senders] == [2, 2])
        await until(lambda: sum(t.done() for t in tasks) == 16)
        total = sum(x.started for x in senders)
        for x in senders:
            x.release.set()
        await asyncio.gather(*tasks)
        return total
    assert asyncio.run(scenario()) == 2 * 2


def test_the_defaults_are_the_documented_ones():
    assert svc_mod.RESET_EMAIL_SLOTS == 4 and svc_mod.RESET_EMAIL_DEADLINE_SECONDS == 15.0
    s = AuthService(Settings(db_enabled=False), MemoryRepo(), GatedSender(), None)
    assert s.reset_slots._value == 4


def test_an_unknown_account_uses_a_slot_briefly_and_sends_nothing():
    sender = GatedSender()
    s = make_service(sender, slots=1)

    async def no_user(email):
        s.repo.lookups.append(email)
        return None
    s.repo.user_by_email = no_user

    async def scenario():
        await s.send_reset_link("ghost" + DOMAIN, "203.0.113.9")
        assert not s.reset_slots.locked()
    asyncio.run(scenario())
    assert sender.started == 0 and s.repo.tokens == []
