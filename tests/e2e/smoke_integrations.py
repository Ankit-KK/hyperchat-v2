"""Opt-in checks of the external integrations that the DEV smoke suite reports BLOCKED: Razorpay (test mode) and
Resend email. Every check is OFF by default; with no opt-in variable this script reads no settings and makes no
network call at all.

Run on the VPS from a checkout, with the release's venv (each check needs the owner's approval first):
    /opt/hyperchat/current/venv/bin/python tests/e2e/smoke_integrations.py
Environment (nothing else named HYPERCHAT_* may be set: it would override the settings file):
    HYPERCHAT_SMOKE_ENV_FILE        only /etc/hyperchat/api.env (the default), by its canonical path
    HYPERCHAT_SMOKE_RAZORPAY=1      D7 credential probe: GET /payments?count=1 (no order, no payment); rzp_test_ only
    HYPERCHAT_SMOKE_RAZORPAY_ORDER=1  additionally ONE test-mode order of INR 1.00 (receipt authtest-int-<tag>)
    HYPERCHAT_SMOKE_EMAIL_TO=addr   ONE delivery-check message to addr, only if addr is in
    HYPERCHAT_SMOKE_EMAIL_ALLOW     exact addresses, comma-separated (no wildcards, no reserved/synthetic domains),
                                    sent from the approved DEV sender no-reply@onboarding.quicktestspace.site
Evidence: PASS / FAIL / SKIPPED / REFUSED lines with the key MODE, the order id and amount, the masked recipient and a
run tag to find the message. Key ids, secrets and the full recipient are never printed: provider errors are reduced to
a reason code or an exception type, and any line that would carry one of them is withheld before it is printed.
Exit 0 = every enabled check passed (skipped checks are fine); 1 = a FAIL or REFUSED.
"""

import asyncio
import logging
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import integration_policy as ip  # noqa: E402


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


def reason_of(exc: BaseException) -> str:
    """A provider error as a reason code (RazorpayError.reason) or the exception type: never its message."""
    reason = getattr(exc, "reason", None)
    return reason if isinstance(reason, str) and reason.isidentifier() else type(exc).__name__


async def check_razorpay(settings, env, report: Report, client_factory, tag: str) -> None:
    from app.payments.accounts import AccountHealth, PaymentAccounts, health_report
    key_id = settings.razorpay_credentials("primary")[0]
    decision, reason = ip.razorpay_plan(env, settings.app_env, key_id)
    if decision != "run":
        report.line("SKIPPED" if decision == "skip" else "REFUSED", "razorpay", reason)
        return
    client = client_factory()
    accounts = PaymentAccounts.from_settings(settings)
    try:
        result = await health_report(accounts, AccountHealth(client))
    except Exception as exc:  # noqa: BLE001 - reported, never retried
        report.line("FAIL", "razorpay", f"D7 credential probe: {reason_of(exc)}")
        return
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
        report.line("FAIL", "razorpay", f"test order: {reason_of(exc)}")
        return
    report.line("PASS", "razorpay", f"test order created: id={order.id} amount={order.amount} {order.currency} "
                                     f"status={order.status} receipt=authtest-int-{tag}")


async def check_email(settings, env, report: Report, sender_factory, tag: str) -> None:
    from app.auth.email import Email
    key = settings.resend_api_key.get_secret_value() if settings.resend_api_key else ""
    decision, reason, to = ip.email_plan(env, settings.app_env, settings.email_backend, bool(key), settings.email_from)
    if decision != "run":
        report.line("SKIPPED" if decision == "skip" else "REFUSED", "email", reason)
        return
    subject = f"[HyperChat DEV] delivery check {tag}"
    body = f"HyperChat DEV email delivery check, run {tag}. No action is needed."
    try:
        await sender_factory(key, settings.email_from).send(
            Email(to=to, template="delivery-check", subject=subject, html=f"<p>{body}</p>", text=body))
    except Exception as exc:  # noqa: BLE001 - reported, never retried
        report.line("FAIL", "email", f"provider refused the message ({reason_of(exc)}) to {ip.mask(to)}")
        return
    report.line("PASS", "email", f"accepted by the provider for {ip.mask(to)} from {ip.APPROVED_SENDER}; "
                                  f"find the subject '{subject}'")


def main(env=None, settings_factory=None, client_factory=None, sender_factory=None, out=print,
         approved_env_file: str = ip.DEV_ENV_FILE) -> int:
    env = dict(os.environ if env is None else env)
    report = Report(out)
    refusal = ip.environment_refusal(env)
    if refusal:
        report.line("REFUSED", "setup", refusal)
        return 1
    if not ip.any_enabled(env):  # the default: no settings read, no client created, no network call
        report.line("SKIPPED", "razorpay", "opt-in: HYPERCHAT_SMOKE_RAZORPAY=1 not set")
        report.line("SKIPPED", "email", "opt-in: HYPERCHAT_SMOKE_EMAIL_TO not set")
        return 0
    refusal = ip.env_file_refusal(env, approved_env_file)
    if refusal:
        report.line("REFUSED", "setup", refusal)
        return 1
    for name in ("httpx", "httpcore"):  # request lines stay out of the output
        logging.getLogger(name).setLevel(logging.WARNING)
    if settings_factory is None:
        from app.core.config import Settings
        settings_factory = lambda path: Settings(_env_file=path)  # noqa: E731
    try:
        settings = settings_factory(approved_env_file)
    except Exception as exc:  # noqa: BLE001 - the type only: a settings error may quote a value
        report.line("REFUSED", "setup", f"the settings could not be loaded ({type(exc).__name__})")
        return 1
    report.secrets = [*settings.razorpay_credentials("primary"),
                      settings.resend_api_key.get_secret_value() if settings.resend_api_key else "",
                      env.get("HYPERCHAT_SMOKE_EMAIL_TO", "").strip()]
    refusal = ip.settings_refusal(settings.app_env, settings.razorpay_credentials("primary")[0])
    if refusal:
        report.line("REFUSED", "setup", refusal)
        return 1
    if client_factory is None:
        from app.payments.razorpay import RazorpayClient
        client_factory = RazorpayClient
    if sender_factory is None:
        from app.auth.email import ResendSender
        sender_factory = ResendSender
    tag = secrets.token_hex(4)
    out(f"integration checks: run {tag}, environment {settings.app_env}")
    for check, factory in ((check_razorpay, client_factory), (check_email, sender_factory)):
        try:
            asyncio.run(check(settings, env, report, factory, tag))
        except Exception as exc:  # noqa: BLE001 - never a traceback: it could carry a value
            report.line("FAIL", check.__name__.removeprefix("check_"), f"unexpected error ({reason_of(exc)})")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
