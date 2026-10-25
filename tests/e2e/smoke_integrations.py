"""Opt-in checks of the external integrations that the DEV smoke suite reports BLOCKED: Razorpay (test mode) and
Resend email. Every check is OFF by default; with no opt-in variable this script makes no network call at all.

Run on the VPS from a checkout, with the release's venv (each check needs the owner's approval first):
    /opt/hyperchat/current/venv/bin/python tests/e2e/smoke_integrations.py
Environment:
    HYPERCHAT_SMOKE_ENV_FILE        /etc/hyperchat/api.env (the DEV API's settings; never the production file)
    HYPERCHAT_SMOKE_RAZORPAY=1      D7 credential probe: GET /payments?count=1 (no order, no payment); rzp_test_ only
    HYPERCHAT_SMOKE_RAZORPAY_ORDER=1  additionally ONE test-mode order of INR 1.00 (receipt authtest-int-<tag>)
    HYPERCHAT_SMOKE_EMAIL_TO=addr   ONE delivery-check message to addr, only if addr is in
    HYPERCHAT_SMOKE_EMAIL_ALLOW     exact addresses, comma-separated (no wildcards, no reserved/synthetic domains)
Evidence: PASS / FAIL / SKIPPED / REFUSED lines with the key MODE, the order id and amount, the masked recipient and a
run tag to find the message. Key ids, secrets and the full recipient are never printed; the output is checked for
them before the script ends. Exit 0 = every enabled check passed (skipped checks are fine); 1 = a FAIL or REFUSED.
"""

import asyncio
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import integration_policy as ip  # noqa: E402

PRODUCTION_ENV_FILES = ("/etc/hyperchat-prod/api.env",)


class Report:
    """Evidence lines; a line that would contain a secret value is withheld before it is printed (and fails)."""

    def __init__(self, out=print) -> None:
        self.lines: list[str] = []
        self.failed = False
        self.secrets: list[str] = []
        self._out = out

    def line(self, kind: str, section: str, text: str) -> None:
        entry = f"{kind:8} [{section}] {text}"
        if ip.leaks(entry, self.secrets):
            kind, entry = "FAIL", f"{'FAIL':8} [evidence] a {section} line was withheld: it contained a secret value"
        if kind in ("FAIL", "REFUSED"):
            self.failed = True
        self.lines.append(entry)
        self._out(entry)


async def check_razorpay(settings, env, report: Report, client_factory, tag: str) -> None:
    from app.payments.accounts import AccountHealth, PaymentAccounts, health_report
    key_id = settings.razorpay_credentials("primary")[0]
    decision, reason = ip.razorpay_plan(env, settings.app_env, key_id)
    if decision != "run":
        report.line("SKIPPED" if decision == "skip" else "REFUSED", "razorpay", reason)
        return
    client = client_factory()
    accounts = PaymentAccounts.from_settings(settings)
    result = await health_report(accounts, AccountHealth(client))
    ok = result["configured"] and result["mode"] == "test" and result["status"] == "ok"
    report.line("PASS" if ok else "FAIL", "razorpay", f"D7 credential probe: mode={result['mode']} status={result['status']}")
    if not ok or not ip.order_wanted(env):
        if ip.order_wanted(env):
            report.line("SKIPPED", "razorpay", "test order not attempted after a failed probe")
        return
    try:
        order = await client.create_order(accounts.for_payments("primary"), amount=100, currency="INR",
                                          receipt=f"authtest-int-{tag}")
    except Exception as exc:  # noqa: BLE001 - reported, never retried
        report.line("FAIL", "razorpay", f"test order: {getattr(exc, 'reason', type(exc).__name__)}")
        return
    report.line("PASS", "razorpay", f"test order created: id={order.id} amount={order.amount} {order.currency} "
                                     f"status={order.status} receipt=authtest-int-{tag}")


async def check_email(settings, env, report: Report, sender_factory, tag: str) -> None:
    from app.auth.email import Email
    key = settings.resend_api_key.get_secret_value() if settings.resend_api_key else ""
    decision, reason, to = ip.email_plan(env, settings.app_env, settings.email_backend, bool(key))
    if decision != "run":
        report.line("SKIPPED" if decision == "skip" else "REFUSED", "email", reason)
        return
    subject = f"[HyperChat DEV] delivery check {tag}"
    body = f"HyperChat DEV email delivery check, run {tag}. No action is needed."
    try:
        await sender_factory(key, settings.email_from).send(
            Email(to=to, template="delivery-check", subject=subject, html=f"<p>{body}</p>", text=body))
    except Exception as exc:  # noqa: BLE001 - reported, never retried
        report.line("FAIL", "email", f"provider refused the message ({type(exc).__name__}) to {ip.mask(to)}")
        return
    report.line("PASS", "email", f"accepted by the provider for {ip.mask(to)}; find the subject '{subject}'")


def main(env=None, settings_factory=None, client_factory=None, sender_factory=None, out=print) -> int:
    env = dict(os.environ if env is None else env)
    env_file = env.get("HYPERCHAT_SMOKE_ENV_FILE", "/etc/hyperchat/api.env")
    report = Report(out)
    if env_file in PRODUCTION_ENV_FILES:
        report.line("REFUSED", "setup", "the production settings file is never used")
        return 1
    if settings_factory is None:
        from app.core.config import Settings
        settings_factory = lambda: Settings(_env_file=env_file)  # noqa: E731
    if client_factory is None:
        from app.payments.razorpay import RazorpayClient
        client_factory = RazorpayClient
    if sender_factory is None:
        from app.auth.email import ResendSender
        sender_factory = ResendSender
    settings = settings_factory()
    if settings.app_env == "production":
        report.line("REFUSED", "setup", "these checks never run with production settings")
        return 1
    report.secrets = [*settings.razorpay_credentials("primary"),
                      settings.resend_api_key.get_secret_value() if settings.resend_api_key else "",
                      env.get("HYPERCHAT_SMOKE_EMAIL_TO", "").strip().lower()]
    tag = secrets.token_hex(4)
    out(f"integration checks: run {tag}, environment {settings.app_env}")
    asyncio.run(check_razorpay(settings, env, report, client_factory, tag))
    asyncio.run(check_email(settings, env, report, sender_factory, tag))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
