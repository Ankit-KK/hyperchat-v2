"""tests/e2e/smoke_integrations.py: the opt-in Razorpay and Resend checks, with fakes only (no network, no real
settings file). Every check is off by default, refuses an unapproved settings file, overriding environment, non-
development settings, live keys and unlisted recipients before any call, and never prints a secret, a key id or the
full recipient."""

import os
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
SENDER = "HyperChat <no-reply@onboarding.quicktestspace.site>"
RAZORPAY = {"HYPERCHAT_SMOKE_RAZORPAY": "1"}
EMAIL = {"HYPERCHAT_SMOKE_EMAIL_TO": OWNER, "HYPERCHAT_SMOKE_EMAIL_ALLOW": OWNER}


def settings(key_id=TEST_KEY, app_env="development", backend="resend", resend=RESEND, sender=SENDER):
    creds = {"primary": (key_id, SECRET if key_id else "", WEBHOOK if key_id else "")}
    return SimpleNamespace(app_env=app_env, email_backend=backend, email_from=sender,
                           resend_api_key=SecretStr(resend) if resend else None,
                           razorpay_credentials=lambda name: creds[name])


@pytest.fixture
def env_file(tmp_path):
    """The stand-in for /etc/hyperchat/api.env: a canonical regular file, 0640."""
    d = tmp_path / "etc-hyperchat"
    d.mkdir()
    f = d / "api.env"
    f.write_text("HYPERCHAT_APP_ENV=development\n")
    os.chmod(f, 0o640)
    return str(Path(os.path.realpath(f)))


class FakeRazorpay:
    def __init__(self, probe="ok", order_id="order_TESTabc123", raise_on=None):
        self.calls, self._probe, self._order_id, self._raise = [], probe, order_id, raise_on

    async def probe(self, account):
        self.calls.append(("probe", account.key_id))
        if self._raise == "probe":
            raise RuntimeError(f"401 for key {account.key_id}:{account.key_secret.get_secret_value()}")
        return self._probe

    async def create_order(self, account, *, amount, currency, receipt):
        self.calls.append(("order", amount, currency, receipt))
        if self._raise == "order":
            raise RuntimeError(f"refused key {account.key_id} secret {SECRET}")
        return Order(self._order_id, amount, currency, "created")


class FakeSender:
    sent: list = []
    fail = False

    def __init__(self, key, sender):
        FakeSender.sent.append(("init", key, sender))

    async def send(self, email):
        if FakeSender.fail:
            raise RuntimeError(f"Resend 403: key {RESEND} cannot send to {email.to}")
        FakeSender.sent.append(("send", email))


def never(*a, **k):
    raise AssertionError("nothing may be created or loaded here")


def run(env, st=None, client=None, sender=never, approved=None, loader=None):
    out: list[str] = []
    code = si.main(env=env, settings_factory=loader or (lambda path: st or settings()),
                   client_factory=(lambda: client) if client else never, sender_factory=sender, out=out.append,
                   **({"approved_env_file": approved} if approved else {}))
    return code, "\n".join(out)


def no_secret(text):
    for value in (TEST_KEY, LIVE_KEY, SECRET, WEBHOOK, RESEND, OWNER, OWNER.upper()):
        assert value.lower() not in text.lower(), value


# ── off by default ──────────────────────────────────────────────────────────────

def test_everything_is_off_by_default_no_settings_read_no_client_no_call():
    code, out = run({}, loader=never)
    assert code == 0 and "SKIPPED  [razorpay]" in out and "SKIPPED  [email]" in out
    no_secret(out)


# ── the environment of the run ──────────────────────────────────────────────────

@pytest.mark.parametrize("env,reason", [
    ({**RAZORPAY, "HYPERCHAT_RAZORPAY_PRIMARY_KEY_ID": LIVE_KEY}, "would override the settings file: HYPERCHAT_RAZORPAY_PRIMARY_KEY_ID"),
    ({**RAZORPAY, "HYPERCHAT_APP_ENV": "development"}, "would override the settings file: HYPERCHAT_APP_ENV"),
    ({**EMAIL, "HYPERCHAT_RESEND_API_KEY": RESEND}, "would override the settings file: HYPERCHAT_RESEND_API_KEY"),
    ({**RAZORPAY, "HYPERCHAT_SMOKE_API": "https://api.hyperchat.space"}, "unexpected variables for these checks: HYPERCHAT_SMOKE_API"),
    ({"HYPERCHAT_SMOKE_RAZORPAY": "yes"}, "HYPERCHAT_SMOKE_RAZORPAY must be 1 or unset"),
    ({"HYPERCHAT_SMOKE_RAZORPAY": "true"}, "HYPERCHAT_SMOKE_RAZORPAY must be 1 or unset"),
    ({"HYPERCHAT_SMOKE_RAZORPAY": "0"}, "HYPERCHAT_SMOKE_RAZORPAY must be 1 or unset"),
    ({**RAZORPAY, "HYPERCHAT_SMOKE_RAZORPAY_ORDER": "on"}, "HYPERCHAT_SMOKE_RAZORPAY_ORDER must be 1 or unset"),
])
def test_unexpected_environment_values_are_refused_before_anything_is_read(env, reason):
    code, out = run(env, loader=never)
    assert code == 1 and "REFUSED  [setup]" in out and reason in out
    no_secret(out)


# ── the settings file: only the approved DEV file, by its canonical path ────────

def test_the_production_file_or_any_other_path_is_refused(env_file):
    for other in ("/etc/hyperchat-prod/api.env", "/etc/hyperchat/api.env.bak", env_file + " ", ""):
        code, out = run({**RAZORPAY, "HYPERCHAT_SMOKE_ENV_FILE": other}, approved=env_file, loader=never)
        assert code == 1 and "may only be the approved DEV settings file" in out, other


def test_an_alias_of_the_approved_path_is_refused(env_file):
    d, name = os.path.split(env_file)
    for alias in (f"{d}/../{os.path.basename(d)}/{name}", f"{d}/./{name}", "/" + env_file):
        code, out = run({**RAZORPAY, "HYPERCHAT_SMOKE_ENV_FILE": alias}, approved=env_file, loader=never)
        assert code == 1 and "REFUSED  [setup]" in out, alias


def test_the_approved_path_as_a_symlink_to_production_is_refused(tmp_path):
    prod = tmp_path / "etc-hyperchat-prod" / "api.env"
    prod.parent.mkdir()
    prod.write_text("HYPERCHAT_APP_ENV=production\n")
    link_dir = tmp_path / "etc-hyperchat"
    link_dir.mkdir()
    approved = str(Path(os.path.realpath(link_dir)) / "api.env")
    os.symlink(prod, approved)
    code, out = run(RAZORPAY, approved=approved, loader=never)
    assert code == 1 and "not canonical" in out


def test_a_symlinked_directory_on_the_way_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "api.env").write_text("x\n")
    os.chmod(real / "api.env", 0o640)
    os.symlink(real, tmp_path / "etc-hyperchat")
    approved = str(Path(os.path.realpath(tmp_path)) / "etc-hyperchat" / "api.env")
    code, out = run(RAZORPAY, approved=approved, loader=never)
    assert code == 1 and "not canonical" in out


def test_a_missing_file_a_directory_or_a_writable_file_is_refused(tmp_path, env_file):
    missing = str(Path(os.path.realpath(tmp_path)) / "absent.env")
    code, out = run(RAZORPAY, approved=missing, loader=never)
    assert code == 1 and "does not exist" in out
    a_dir = Path(os.path.realpath(tmp_path)) / "dir.env"
    a_dir.mkdir()
    code, out = run(RAZORPAY, approved=str(a_dir), loader=never)
    assert code == 1 and "not a regular file" in out
    os.chmod(env_file, 0o664)
    code, out = run(RAZORPAY, approved=env_file, loader=never)
    assert code == 1 and "writable by group or others" in out


def test_a_settings_loading_failure_is_refused_without_its_message(env_file):
    def broken(path):
        raise ValueError(f"Refusing to start: HYPERCHAT_RAZORPAY_PRIMARY_KEY_SECRET={SECRET} for {OWNER}")
    code, out = run(RAZORPAY, approved=env_file, loader=broken)
    assert code == 1 and "the settings could not be loaded (ValueError)" in out
    no_secret(out)


@pytest.mark.parametrize("st,reason", [
    (settings(app_env="production"), "not development (production)"),
    (settings(app_env="test"), "not development (test)"),
    (settings(key_id=LIVE_KEY), "a live Razorpay key is configured"),
    (settings(key_id="key_unknown_1234"), "a unknown Razorpay key is configured"),
])
def test_non_development_settings_or_a_non_test_key_stop_every_check(env_file, st, reason):
    for env in (RAZORPAY, EMAIL):   # even the email check never runs next to a live key
        code, out = run(env, st, approved=env_file)
        assert code == 1 and "REFUSED  [setup]" in out and reason in out
        no_secret(out)


def test_the_loader_receives_exactly_the_approved_path(env_file):
    seen = []
    code, _ = run(RAZORPAY, client=FakeRazorpay(), approved=env_file, loader=lambda p: seen.append(p) or settings())
    assert code == 0 and seen == [env_file]


# ── Razorpay (test mode) ────────────────────────────────────────────────────────

def test_razorpay_refuses_a_missing_key_before_any_call(env_file):
    code, out = run(RAZORPAY, settings(key_id=""), approved=env_file)
    assert code == 1 and "no Razorpay key configured" in out


def test_an_order_needs_the_razorpay_opt_in_too(env_file):
    code, out = run({"HYPERCHAT_SMOKE_RAZORPAY_ORDER": "1"}, approved=env_file)
    assert code == 1 and "needs HYPERCHAT_SMOKE_RAZORPAY=1" in out


def test_the_probe_alone_creates_no_order_and_prints_the_mode_only(env_file):
    rp = FakeRazorpay()
    code, out = run(RAZORPAY, client=rp, approved=env_file)
    assert code == 0 and rp.calls == [("probe", TEST_KEY)]
    assert "PASS     [razorpay] D7 credential probe: mode=test status=ok" in out
    no_secret(out)


def test_one_test_order_of_inr_1_with_a_tagged_receipt(env_file):
    rp = FakeRazorpay()
    code, out = run({**RAZORPAY, "HYPERCHAT_SMOKE_RAZORPAY_ORDER": "1"}, client=rp, approved=env_file)
    assert code == 0 and [c[0] for c in rp.calls] == ["probe", "order"]
    _, amount, currency, receipt = rp.calls[1]
    assert (amount, currency) == (100, "INR") and receipt.startswith("authtest-int-")
    assert "test order created: id=order_TESTabc123 amount=100 INR status=created" in out
    no_secret(out)


def test_a_failed_probe_is_a_fail_and_no_order_is_attempted(env_file):
    rp = FakeRazorpay(probe="invalid_key")
    code, out = run({**RAZORPAY, "HYPERCHAT_SMOKE_RAZORPAY_ORDER": "1"}, client=rp, approved=env_file)
    assert code == 1 and rp.calls == [("probe", TEST_KEY)] and "status=invalid_key" in out


@pytest.mark.parametrize("where", ["probe", "order"])
def test_a_provider_exception_carrying_the_key_prints_only_its_type(env_file, where):
    rp = FakeRazorpay(raise_on=where)
    code, out = run({**RAZORPAY, "HYPERCHAT_SMOKE_RAZORPAY_ORDER": "1"}, client=rp, approved=env_file)
    assert code == 1 and "RuntimeError" in out
    no_secret(out)


def test_a_line_carrying_a_secret_is_withheld_and_fails(env_file):
    rp = FakeRazorpay(order_id="order_" + SECRET.upper())   # a hostile or broken response echoing the secret
    code, out = run({**RAZORPAY, "HYPERCHAT_SMOKE_RAZORPAY_ORDER": "1"}, client=rp, approved=env_file)
    assert code == 1 and "withheld: it contained a secret value" in out
    no_secret(out)


# ── Resend email ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("env,reason", [
    ({"HYPERCHAT_SMOKE_EMAIL_TO": OWNER}, "HYPERCHAT_SMOKE_EMAIL_ALLOW (exact addresses) is required"),
    ({"HYPERCHAT_SMOKE_EMAIL_TO": OWNER, "HYPERCHAT_SMOKE_EMAIL_ALLOW": "someone.else@gmail.com"}, "not on HYPERCHAT_SMOKE_EMAIL_ALLOW"),
    ({"HYPERCHAT_SMOKE_EMAIL_TO": OWNER, "HYPERCHAT_SMOKE_EMAIL_ALLOW": "*@gmail.com"}, "exact addresses only"),
    ({"HYPERCHAT_SMOKE_EMAIL_TO": OWNER, "HYPERCHAT_SMOKE_EMAIL_ALLOW": "@gmail.com"}, "exact addresses only"),
    ({"HYPERCHAT_SMOKE_EMAIL_TO": f"{OWNER},x@y.org", "HYPERCHAT_SMOKE_EMAIL_ALLOW": f"{OWNER},x@y.org"}, "not on HYPERCHAT_SMOKE_EMAIL_ALLOW"),
    ({"HYPERCHAT_SMOKE_EMAIL_TO": "x@authtest.invalid", "HYPERCHAT_SMOKE_EMAIL_ALLOW": "x@authtest.invalid"}, "reserved or synthetic"),
    ({"HYPERCHAT_SMOKE_EMAIL_TO": "x@example.com", "HYPERCHAT_SMOKE_EMAIL_ALLOW": "x@example.com"}, "reserved or synthetic"),
    ({"HYPERCHAT_SMOKE_EMAIL_TO": "x@host.test", "HYPERCHAT_SMOKE_EMAIL_ALLOW": "x@host.test"}, "reserved or synthetic"),
    ({"HYPERCHAT_SMOKE_EMAIL_TO": "not an address", "HYPERCHAT_SMOKE_EMAIL_ALLOW": "not an address"}, "exact addresses only"),
])
def test_email_refuses_without_an_exact_allowlisted_real_recipient(env_file, env, reason):
    code, out = run(env, approved=env_file)
    assert code == 1 and "REFUSED  [email]" in out and reason in out
    no_secret(out)


@pytest.mark.parametrize("st,reason", [
    (settings(backend="disabled"), "the email backend is not resend"),
    (settings(resend=""), "the email backend is not resend"),
    (settings(sender="HyperChat <no-reply@hyperchat.space>"), "not the approved DEV sender no-reply@onboarding.quicktestspace.site"),
    (settings(sender="no-reply@quicktestspace.site"), "not the approved DEV sender"),
    (settings(sender="no-reply@onboarding.quicktestspace.site.evil.com"), "not the approved DEV sender"),
    (settings(sender=""), "not the approved DEV sender"),
    # the other planned category domains are not verified: this check never sends from them
    (settings(sender="HyperChat Security <no-reply@auth.quicktestspace.site>"), "not the approved DEV sender"),
    (settings(sender="HyperChat <no-reply@notifications.quicktestspace.site>"), "not the approved DEV sender"),
    (settings(sender="HyperChat Payments <no-reply@payments.quicktestspace.site>"), "not the approved DEV sender"),
    (settings(sender="HyperChat Support <no-reply@support.quicktestspace.site>"), "not the approved DEV sender"),
    (settings(sender="HyperChat <hello@onboarding.quicktestspace.site>"), "not the approved DEV sender"),
])
def test_email_refuses_without_the_resend_backend_key_and_approved_sender(env_file, st, reason):
    code, out = run(EMAIL, st, approved=env_file)
    assert code == 1 and reason in out


@pytest.mark.parametrize("sender", [SENDER, "no-reply@onboarding.quicktestspace.site", "NO-REPLY@onboarding.quicktestspace.site"])
def test_one_message_from_the_approved_sender_to_the_allowlisted_recipient(env_file, sender):
    FakeSender.sent, FakeSender.fail = [], False
    code, out = run({"HYPERCHAT_SMOKE_EMAIL_TO": OWNER.upper(), "HYPERCHAT_SMOKE_EMAIL_ALLOW": f"x@y.org, {OWNER}"},
                    settings(sender=sender), sender=FakeSender, approved=env_file)
    assert code == 0 and [s[0] for s in FakeSender.sent] == ["init", "send"]
    assert FakeSender.sent[0][1:] == (RESEND, sender) and FakeSender.sent[1][1].to == OWNER
    assert "delivery check" in FakeSender.sent[1][1].subject
    assert f"for {ip.mask(OWNER)} from no-reply@onboarding.quicktestspace.site" in out and ip.mask(OWNER) == "o***@gmail.com"
    no_secret(out)


def test_a_provider_error_naming_the_key_and_recipient_prints_neither(env_file):
    FakeSender.sent, FakeSender.fail = [], True
    try:
        code, out = run(EMAIL, sender=FakeSender, approved=env_file)
    finally:
        FakeSender.fail = False
    assert code == 1 and "provider refused the message (RuntimeError) to o***@gmail.com" in out
    no_secret(out)


@pytest.fixture
def real_env_file(env_file, monkeypatch):
    """The real app Settings read the approved file (no HYPERCHAT_* override in this process)."""
    for k in list(os.environ):
        if k.startswith("HYPERCHAT_"):
            monkeypatch.delenv(k)
    Path(env_file).write_text("\n".join([
        "HYPERCHAT_APP_ENV=development", f"HYPERCHAT_RAZORPAY_PRIMARY_KEY_ID={TEST_KEY}",
        f"HYPERCHAT_RAZORPAY_PRIMARY_KEY_SECRET={SECRET}", f"HYPERCHAT_RAZORPAY_PRIMARY_WEBHOOK_SECRET={WEBHOOK}",
        "HYPERCHAT_EMAIL_BACKEND=resend", f"HYPERCHAT_RESEND_API_KEY={RESEND}",
        "HYPERCHAT_EMAIL_FROM=HyperChat <no-reply@onboarding.quicktestspace.site>", ""]))
    return env_file


def test_the_real_settings_loader_reads_only_the_approved_file(real_env_file):
    FakeSender.sent, FakeSender.fail = [], False
    rp, out = FakeRazorpay(), []
    code = si.main(env={**RAZORPAY, **EMAIL}, client_factory=lambda: rp, sender_factory=FakeSender, out=out.append,
                   approved_env_file=real_env_file)
    text = "\n".join(out)
    assert code == 0 and rp.calls == [("probe", TEST_KEY)], text
    assert FakeSender.sent[0][1:] == (RESEND, "HyperChat <no-reply@onboarding.quicktestspace.site>")
    no_secret(text)


def test_the_real_settings_loader_refuses_a_file_with_the_production_sender(real_env_file):
    Path(real_env_file).write_text(Path(real_env_file).read_text().replace(
        "HyperChat <no-reply@onboarding.quicktestspace.site>", "HyperChat <no-reply@hyperchat.space>"))
    out: list[str] = []
    code = si.main(env=EMAIL, client_factory=never, sender_factory=never, out=out.append, approved_env_file=real_env_file)
    assert code == 1 and "not the approved DEV sender" in "\n".join(out)


def test_the_policy_never_runs_outside_development():
    for app_env in ("production", "test"):
        assert ip.razorpay_plan(RAZORPAY, app_env, TEST_KEY)[0] == "refuse"
        assert ip.email_plan(EMAIL, app_env, "resend", True, SENDER)[0] == "refuse"
    assert ip.key_mode(TEST_KEY) == "test" and ip.key_mode(LIVE_KEY) == "live"
    assert ip.DEV_ENV_FILE == "/etc/hyperchat/api.env" and ip.APPROVED_SENDER == "no-reply@onboarding.quicktestspace.site"
