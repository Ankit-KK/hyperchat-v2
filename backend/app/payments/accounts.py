"""The configured Razorpay accounts ('primary', 'secondary') and their credential health.

An account can take payments when its key id and key secret are set; it can receive webhooks when its
webhook secret is set. Health is per account (creators share the two key sets), probed with a cheap
authenticated read and cached for HEALTH_TTL_SECONDS. A network error is not cached (the next checkout
probes again, as legacy re-probed network errors), so an outage lasts no longer than Razorpay's.
"""

import asyncio
import time

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

    def forget(self, name: str) -> None:
        self._cache.pop(name, None)

    def mark(self, name: str, status: str) -> None:
        """Record a status learned elsewhere (e.g. Razorpay refused the keys while creating an order)."""
        self._cache[name] = (self._clock() + HEALTH_TTL_SECONDS, status)

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
            return status
