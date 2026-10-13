"""D7: manual Razorpay health check (admin endpoint + `python -m app.cli payment-health`) through the same cached probe
checkout uses, and the transient answer at checkout when Razorpay is unreachable. Razorpay is always faked."""

import asyncio
from datetime import datetime

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.payments.accounts import AccountHealth, PaymentAccounts, health_report, key_mode
from app.payments.razorpay import RazorpayAccount
from tests.integration import DOMAIN, ORIGIN, new_browser, requires_db, signed_in
from tests.payments.fakes import FakeRazorpay
from tests.payments.test_payments_db import client, rz_env  # noqa: F401 (fixtures)

H = "/api/v1/admin/payments/health"
KEYS = {"razorpay_primary_key_id": "rzp_test_TESTONLYkey", "razorpay_primary_key_secret": "TEST-ONLY-secret-1",
        "razorpay_primary_webhook_secret": "TEST-ONLY-webhook-1"}


class Probe:
    def __init__(self, *results):
        self.results, self.calls = list(results), 0

    async def probe(self, account):
        self.calls += 1
        return self.results.pop(0)


def accounts(**keys) -> PaymentAccounts:
    return PaymentAccounts.from_settings(Settings(_env_file=None, **keys))


def no_secrets(text: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in text


# ── the shared report ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("key, mode", [("rzp_test_x", "test"), ("rzp_live_x", "live"), ("other", "unknown"),
                                       ("", "unknown")])
def test_key_mode(key, mode):
    assert key_mode(key) == mode


@pytest.mark.parametrize("result", ["ok", "invalid_key", "network_error"])
def test_report_reuses_the_probe_and_shows_names_and_states_only(result):
    probe = Probe(result)
    report = asyncio.run(health_report(accounts(**KEYS), AccountHealth(probe)))
    assert {k: report[k] for k in ("account", "configured", "mode", "status")} == {
        "account": "primary", "configured": True, "mode": "test", "status": result}
    assert datetime.fromisoformat(report["checked_at"]) and probe.calls == 1
    no_secrets(str(report), *KEYS.values())


def test_report_for_a_live_key_and_for_missing_keys():
    live = PaymentAccounts({"primary": RazorpayAccount("primary", "rzp_live_TESTONLY", SecretStr("TEST-ONLY-s"), None)})
    assert asyncio.run(health_report(live, AccountHealth(Probe("ok"))))["mode"] == "live"
    probe = Probe()
    report = asyncio.run(health_report(accounts(), AccountHealth(probe)))
    assert {k: report[k] for k in ("configured", "mode", "status")} == {
        "configured": False, "mode": "unknown", "status": "missing_key"} and probe.calls == 0


def test_report_respects_the_cache_and_keeps_the_original_check_time():
    probe = Probe("ok")
    health, acc = AccountHealth(probe), accounts(**KEYS)
    first = asyncio.run(health_report(acc, health))
    second = asyncio.run(health_report(acc, health))
    assert probe.calls == 1 and first["checked_at"] == second["checked_at"]


# ── CLI ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("result, code", [("ok", 0), ("invalid_key", 1), ("network_error", 1), (None, 1)])
def test_cli_payment_health(monkeypatch, capsys, result, code):
    from app import cli

    for key, value in (KEYS if result else {}).items():
        monkeypatch.setenv(f"HYPERCHAT_{key.upper()}", value)
    if not result:
        for key in KEYS:
            monkeypatch.delenv(f"HYPERCHAT_{key.upper()}", raising=False)
    monkeypatch.setenv("HYPERCHAT_DB_ENABLED", "false")  # no database needed
    fake = FakeRazorpay()
    fake.probe_result = result or "ok"
    monkeypatch.setattr(cli, "RazorpayClient", lambda: fake)
    assert cli.main(["payment-health"]) == code
    out = capsys.readouterr()
    status = result or "missing_key"
    assert "account=primary" in out.out and f"status={status}" in out.out
    assert ("mode=test" if result else "mode=unknown") in out.out
    no_secrets(out.out + out.err, *KEYS.values())
    assert fake.count("create_order") == 0 and fake.count("probe") == (1 if result else 0)


# ── admin endpoint ──────────────────────────────────────────────────────────────

def _admin(app):
    return signed_in(app, "payhealth-admin" + DOMAIN, role="admin")


@requires_db
def test_admin_endpoint_reports_and_respects_the_cache(client):
    _admin(client)
    r = client.get(H)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {"account", "configured", "mode", "status", "checked_at"}
    assert {k: body[k] for k in ("account", "configured", "mode", "status")} == {
        "account": "primary", "configured": True, "mode": "test", "status": "ok"}
    assert client.get(H).json()["checked_at"] == body["checked_at"]  # cached: one probe for both calls
    assert client.fake.count("probe") == 1 and client.fake.count("create_order") == 0
    no_secrets(r.text, *client.env.values())
    logged = "\n".join(client.app_state.log_capture.records)
    no_secrets(logged, *client.env.values())
    assert "payment_health_checked" in logged


@requires_db
@pytest.mark.parametrize("result", ["invalid_key", "network_error"])
def test_admin_endpoint_reports_failures_without_upstream_detail(client, result):
    client.fake.probe_result = result
    _admin(client)
    r = client.get(H)
    assert r.status_code == 200 and r.json()["status"] == result and r.json()["configured"] is True
    no_secrets(r.text, *client.env.values())


@requires_db
def test_admin_endpoint_without_configured_keys(client):
    client.app_state.payments.accounts = PaymentAccounts({})
    _admin(client)
    body = client.get(H).json()
    assert (body["configured"], body["mode"], body["status"]) == (False, "unknown", "missing_key")
    assert client.fake.count("probe") == 0


@requires_db
def test_admin_endpoint_is_admin_only(client):
    assert new_browser(client).get(H).status_code == 401
    signed_in(client, "payhealth-user" + DOMAIN)
    assert client.get(H).status_code == 403
    assert client.get(H, headers=ORIGIN).status_code == 403
    assert client.fake.count("probe") == 0


# ── checkout: invalid / missing keys hold, an unreachable Razorpay is transient ─

@requires_db
@pytest.mark.parametrize("result, status, error", [
    ("invalid_key", 503, "Payments for this creator are on hold"),
    ("network_error", 502, "Payment provider unavailable. Please try again."),
])
def test_checkout_answers(client, result, status, error):
    from tests.integration import db, make_streamer

    make_streamer("authtest-payhealth")
    client.fake.probe_result = result
    r = client.post("/api/v1/public/streamers/authtest-payhealth/donations", json={"name": "Ravi", "amount": 100},
                    headers=ORIGIN)
    assert (r.status_code, r.json()) == (status, {"error": error}) and client.fake.count("create_order") == 0
    with db() as conn:  # the stored health keeps the real reason
        assert conn.execute("SELECT h.status FROM public.streamer_payment_health h JOIN public.streamers s "
                            "ON s.id = h.streamer_id WHERE s.streamer_slug = 'authtest-payhealth'").fetchone()[0] \
            == result
