"""tests/e2e/smoke_integrations.py: the opt-in Razorpay and Resend checks, with fakes only (no network). Every check is
off by default, refuses live keys and unlisted recipients before any call, and never prints a secret."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "e2e"))
import integration_policy as ip  # noqa: E402
import smoke_integrations as si  # noqa: E402

from app.payments.razorpay import Order  # noqa: E402

TEST_KEY, LIVE_KEY, SECRET, WEBHOOK = "rzp_test_AbCdEf123456", "rzp_live_AbCdEf123456", "s3cr3t-key-value-xyz", "whsec-value-123456"
RESEND = "re_resend_api_key_value_123"
OWNER = "owner.inbox@gmail.com"


def settings(key_id=TEST_KEY, app_env="development", backend="resend", resend=RESEND):
    creds = {"primary": (key_id, SECRET if key_id else "", WEBHOOK if key_id else "")}
    return SimpleNamespace(app_env=app_env, email_backend=backend, email_from="HyperChat DEV <dev@quicktestspace.site>",
                           resend_api_key=SecretStr(resend) if resend else None,
                           razorpay_credentials=lambda name: creds[name])


class FakeRazorpay:
    def __init__(self, probe="ok", order_id="order_TESTabc123"):
        self.calls, self._probe, self._order_id = [], probe, order_id

    async def probe(self, account):
        self.calls.append(("probe", account.key_id))
        return self._probe

    async def create_order(self, account, *, amount, currency, receipt):
        self.calls.append(("order", amount, currency, receipt))
        return Order(self._order_id, amount, currency, "created")


class FakeSender:
    sent: list = []

    def __init__(self, key, sender):
        FakeSender.sent.append(("init", key, sender))

    async def send(self, email):
        FakeSender.sent.append(("send", email))


def never(*a, **k):
    raise AssertionError("no client may be created")


def run(env, st=None, client=None, sender=never):
    out: list[str] = []
    code = si.main(env=env, settings_factory=lambda: st or settings(),
                   client_factory=(lambda: client) if client else never, sender_factory=sender, out=out.append)
    return code, "\n".join(out)


def no_secret(text):
    for value in (TEST_KEY, LIVE_KEY, SECRET, WEBHOOK, RESEND, OWNER):
        assert value not in text, value


def test_everything_is_off_by_default_and_makes_no_call():
    code, out = run({})
    assert code == 0 and "SKIPPED  [razorpay]" in out and "SKIPPED  [email]" in out
    no_secret(out)


@pytest.mark.parametrize("key,reason", [(LIVE_KEY, "found a live key"), ("key_unknown_1234", "found a unknown key"),
                                        ("", "no Razorpay key configured")])
def test_razorpay_refuses_anything_but_a_test_key_before_any_call(key, reason):
    code, out = run({"HYPERCHAT_SMOKE_RAZORPAY": "1"}, settings(key_id=key))
    assert code == 1 and "REFUSED  [razorpay]" in out and reason in out
    no_secret(out)


def test_an_order_needs_the_razorpay_opt_in_too():
    code, out = run({"HYPERCHAT_SMOKE_RAZORPAY_ORDER": "1"})
    assert code == 1 and "needs HYPERCHAT_SMOKE_RAZORPAY=1" in out


def test_the_probe_alone_creates_no_order_and_prints_the_mode_only():
    rp = FakeRazorpay()
    code, out = run({"HYPERCHAT_SMOKE_RAZORPAY": "1"}, client=rp)
    assert code == 0 and rp.calls == [("probe", TEST_KEY)]
    assert "PASS     [razorpay] D7 credential probe: mode=test status=ok" in out
    no_secret(out)


def test_one_test_order_of_inr_1_with_a_tagged_receipt():
    rp = FakeRazorpay()
    code, out = run({"HYPERCHAT_SMOKE_RAZORPAY": "1", "HYPERCHAT_SMOKE_RAZORPAY_ORDER": "1"}, client=rp)
    assert code == 0 and [c[0] for c in rp.calls] == ["probe", "order"]
    _, amount, currency, receipt = rp.calls[1]
    assert (amount, currency) == (100, "INR") and receipt.startswith("authtest-int-")
    assert "test order created: id=order_TESTabc123 amount=100 INR status=created" in out
    no_secret(out)


def test_a_failed_probe_is_a_fail_and_no_order_is_attempted():
    rp = FakeRazorpay(probe="invalid_key")
    code, out = run({"HYPERCHAT_SMOKE_RAZORPAY": "1", "HYPERCHAT_SMOKE_RAZORPAY_ORDER": "1"}, client=rp)
    assert code == 1 and rp.calls == [("probe", TEST_KEY)] and "status=invalid_key" in out


@pytest.mark.parametrize("env,reason", [
    ({"HYPERCHAT_SMOKE_EMAIL_TO": OWNER}, "HYPERCHAT_SMOKE_EMAIL_ALLOW (exact addresses) is required"),
    ({"HYPERCHAT_SMOKE_EMAIL_TO": OWNER, "HYPERCHAT_SMOKE_EMAIL_ALLOW": "someone.else@gmail.com"}, "not on HYPERCHAT_SMOKE_EMAIL_ALLOW"),
    ({"HYPERCHAT_SMOKE_EMAIL_TO": OWNER, "HYPERCHAT_SMOKE_EMAIL_ALLOW": "*@gmail.com"}, "exact addresses only"),
    ({"HYPERCHAT_SMOKE_EMAIL_TO": OWNER, "HYPERCHAT_SMOKE_EMAIL_ALLOW": "@gmail.com"}, "exact addresses only"),
    ({"HYPERCHAT_SMOKE_EMAIL_TO": "x@authtest.invalid", "HYPERCHAT_SMOKE_EMAIL_ALLOW": "x@authtest.invalid"}, "reserved or synthetic"),
    ({"HYPERCHAT_SMOKE_EMAIL_TO": "x@example.com", "HYPERCHAT_SMOKE_EMAIL_ALLOW": "x@example.com"}, "reserved or synthetic"),
    ({"HYPERCHAT_SMOKE_EMAIL_TO": "x@host.test", "HYPERCHAT_SMOKE_EMAIL_ALLOW": "x@host.test"}, "reserved or synthetic"),
    ({"HYPERCHAT_SMOKE_EMAIL_TO": "not an address", "HYPERCHAT_SMOKE_EMAIL_ALLOW": "not an address"}, "exact addresses only"),
])
def test_email_refuses_without_an_exact_allowlisted_real_recipient(env, reason):
    code, out = run(env)
    assert code == 1 and "REFUSED  [email]" in out and reason in out
    no_secret(out)


@pytest.mark.parametrize("st", [settings(backend="disabled"), settings(resend="")])
def test_email_refuses_without_the_resend_backend_and_key(st):
    code, out = run({"HYPERCHAT_SMOKE_EMAIL_TO": OWNER, "HYPERCHAT_SMOKE_EMAIL_ALLOW": OWNER}, st)
    assert code == 1 and "the email backend is not resend" in out


def test_one_message_to_the_allowlisted_recipient_with_masked_evidence():
    FakeSender.sent = []
    code, out = run({"HYPERCHAT_SMOKE_EMAIL_TO": OWNER.upper(), "HYPERCHAT_SMOKE_EMAIL_ALLOW": f"x@y.org, {OWNER}"},
                    sender=FakeSender)
    assert code == 0 and [s[0] for s in FakeSender.sent] == ["init", "send"]
    assert FakeSender.sent[0][1] == RESEND and FakeSender.sent[1][1].to == OWNER
    assert "delivery check" in FakeSender.sent[1][1].subject
    assert f"for {ip.mask(OWNER)}" in out and ip.mask(OWNER) == "o***@gmail.com"
    no_secret(out)


@pytest.mark.parametrize("env,st", [({"HYPERCHAT_SMOKE_RAZORPAY": "1"}, settings(app_env="production")),
                                    ({"HYPERCHAT_SMOKE_ENV_FILE": "/etc/hyperchat-prod/api.env", "HYPERCHAT_SMOKE_RAZORPAY": "1"}, None)])
def test_production_settings_are_refused_before_anything(env, st):
    code, out = run(env, st)
    assert code == 1 and "REFUSED  [setup]" in out


def test_a_line_carrying_a_secret_is_withheld_and_fails():
    rp = FakeRazorpay(order_id="order_" + SECRET)   # a hostile or broken response echoing the secret
    code, out = run({"HYPERCHAT_SMOKE_RAZORPAY": "1", "HYPERCHAT_SMOKE_RAZORPAY_ORDER": "1"}, client=rp)
    assert code == 1 and "withheld: it contained a secret value" in out
    no_secret(out)


def test_the_policy_never_runs_in_production():
    assert ip.razorpay_plan({"HYPERCHAT_SMOKE_RAZORPAY": "1"}, "production", TEST_KEY)[0] == "refuse"
    assert ip.email_plan({"HYPERCHAT_SMOKE_EMAIL_TO": OWNER, "HYPERCHAT_SMOKE_EMAIL_ALLOW": OWNER}, "production", "resend", True)[0] == "refuse"
    assert ip.key_mode(TEST_KEY) == "test" and ip.key_mode(LIVE_KEY) == "live"
