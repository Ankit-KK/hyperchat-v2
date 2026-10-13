"""The configured Razorpay account(s) (D6: only 'primary'; test vs live is per environment) and their health.

An account can take payments when its key id and key secret are set; it can receive webhooks when its
webhook secret is set. Health is per account (every creator shares it), probed with a cheap
authenticated read and cached for HEALTH_TTL_SECONDS. A network error is not cached (the next checkout
probes again, as legacy re-probed network errors), so an outage lasts no longer than Razorpay's.
"""

import asyncio
import time
from datetime import UTC, datetime

from pydantic import SecretStr

from app.core.config import RAZORPAY_ACCOUNTS, Settings
from app.payments.razorpay import RazorpayAccount, RazorpayClient

HEALTH_TTL_SECONDS = 120
HEALTH_STATUSES = ("ok", "invalid_key", "missing_key", "network_error")


class PaymentAccounts:
    def __init__(self, accounts: dict[str, RazorpayAccount]) -> None:
        self._accounts = accounts

    @classmethod
    def from_settings(cls, settings: Settings) -> "PaymentAccounts":
        accounts = {}
        for name in RAZORPAY_ACCOUNTS:
            key_id, key_secret, webhook_secret = settings.razorpay_credentials(name)
            if key_id or key_secret or webhook_secret:
                accounts[name] = RazorpayAccount(name, key_id, SecretStr(key_secret),
                                                 SecretStr(webhook_secret) if webhook_secret else None)
        return cls(accounts)

    def for_payments(self, name: str | None) -> RazorpayAccount | None:
        """The account when it can create orders and fetch payments (key id and secret set)."""
        account = self._accounts.get(name) if name else None
        if account is None or not account.key_id or not account.key_secret.get_secret_value():
            return None
        return account

    def webhook_secrets(self) -> list[tuple[str, str]]:
        return [(name, a.webhook_secret.get_secret_value()) for name, a in self._accounts.items()
                if a.webhook_secret is not None and a.webhook_secret.get_secret_value()]

    def summary(self) -> dict[str, list[str]]:
        """Account names (never values) that can take payments / receive webhooks, for the startup log."""
        return {"payments": [n for n in RAZORPAY_ACCOUNTS if self.for_payments(n)],
                "webhooks": [n for n, _ in self.webhook_secrets()]}


class AccountHealth:
    def __init__(self, client: RazorpayClient, clock=time.monotonic) -> None:
        self.client, self._clock = client, clock
        self._cache: dict[str, tuple[float, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._checked_at: dict[str, datetime] = {}  # when the current answer was learned (wall clock)

    def checked_at(self, name: str) -> datetime | None:
        return self._checked_at.get(name)

    def forget(self, name: str) -> None:
        self._cache.pop(name, None)

    def mark(self, name: str, status: str) -> None:
        """Record a status learned elsewhere (e.g. Razorpay refused the keys while creating an order)."""
        self._cache[name] = (self._clock() + HEALTH_TTL_SECONDS, status)
        self._checked_at[name] = datetime.now(UTC)

    async def status(self, account: RazorpayAccount) -> str:
        hit = self._cache.get(account.name)
        if hit and hit[0] > self._clock():
            return hit[1]
        async with self._locks.setdefault(account.name, asyncio.Lock()):
            hit = self._cache.get(account.name)
            if hit and hit[0] > self._clock():
                return hit[1]
            status = await self.client.probe(account)
            if status != "network_error":
                self.mark(account.name, status)
            else:
                self._checked_at[account.name] = datetime.now(UTC)
            return status


def key_mode(key_id: str) -> str:
    """Razorpay key mode from the key id prefix; the id itself is never shown."""
    return "test" if key_id.startswith("rzp_test_") else "live" if key_id.startswith("rzp_live_") else "unknown"


async def health_report(accounts: PaymentAccounts, health: AccountHealth, name: str = "primary") -> dict:
    """D7 manual check (admin endpoint and `app.cli payment-health`): the account's credential health through the
    same cached probe checkout uses (GET /payments?count=1: no order, no payment). Names and states only."""
    account = accounts.for_payments(name)
    if account is None:
        return {"account": name, "configured": False, "mode": "unknown", "status": "missing_key",
                "checked_at": datetime.now(UTC).isoformat()}
    status = await health.status(account)
    checked = health.checked_at(name) or datetime.now(UTC)
    return {"account": name, "configured": True, "mode": key_mode(account.key_id), "status": status,
            "checked_at": checked.isoformat()}
