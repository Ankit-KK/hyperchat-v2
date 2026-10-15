"""End-to-end smoke suite for the DEPLOYED DEV backend: synthetic, self-cleaning, repeatable.

Runs ON THE VPS, as root, from a checkout of this repository at the deployed commit (it needs peer access to the DEV
database to seed and remove synthetic rows, systemctl and the logs):
    /opt/hyperchat/current/venv/bin/python tests/e2e/smoke_dev_backend.py
Environment (defaults = v2 development):
    HYPERCHAT_SMOKE_API       https://api.quicktestspace.site
    HYPERCHAT_SMOKE_DB        hyperchat_dev:5433          (refuses hyperchat and hyperchat_restore_test)
    HYPERCHAT_SMOKE_ORIGIN    https://app.quicktestspace.site
    HYPERCHAT_SMOKE_LOCAL     http://127.0.0.1:8000       the DEV API's local port (release / environment details)
    HYPERCHAT_SMOKE_ENV_FILE  /etc/hyperchat/api.env      read only (bots flag, public contact values, R2)

Safety: refuses to run unless the API reports environment "development", the database is not a production or
restore database, the API is not api.hyperchat.space, and bots are disabled. It never changes configuration,
never creates a Razorpay order, never writes to R2 (except the approval-gated upload-cleanup section), never sends
email and never calls a TTS or transcription provider; checks that would need any of that are reported as BLOCKED with the credential or approval they need.
Approving a synthetic donation makes the worker publish realtime events to the DEV Pusher app (realtime is on in
DEV); nothing else leaves the server. Every synthetic row is recognizable (users @authtest.invalid, creators
"authtest-*", orders "authtest-*") and removed at the end, and the cleanup is verified. The login rate-limit counters
the suite consumes (auth:* rows) are cleared, as smoke_auth.py does. Nothing secret is printed.
Feature sections (payments-account D6, payment-health D7, admin-email D11, owner-transfer D13, upload-cleanup D9,
public-config D5, ...) report BLOCKED while the deployed release predates them. upload-cleanup also writes and deletes a few synthetic
objects in the DEV R2 bucket, so it runs only with HYPERCHAT_SMOKE_R2=1 (an explicit approval) and otherwise reports
BLOCKED; it runs the deployed cleanup on this run's keys only, with the clock moved 25 hours ahead. The owner-transfer section moves a synthetic creator between two synthetic
users only; no real creator or account is touched.
Exit code: 0 = every runnable check passed (BLOCKED checks do not fail the run), 1 = a check failed, 2 = refused.
"""

import os
import re
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.auth.passwords import hash_password  # noqa: E402
from app.auth.service import GENERIC_LOGIN_ERROR as GENERIC  # noqa: E402
from app.auth.tokens import sha256_hex  # noqa: E402

API = os.environ.get("HYPERCHAT_SMOKE_API", "https://api.quicktestspace.site").rstrip("/")
LOCAL = os.environ.get("HYPERCHAT_SMOKE_LOCAL", "http://127.0.0.1:8000").rstrip("/")
FRONT = os.environ.get("HYPERCHAT_SMOKE_ORIGIN", "https://app.quicktestspace.site").rstrip("/")
DB_NAME, DB_PORT = os.environ.get("HYPERCHAT_SMOKE_DB", "hyperchat_dev:5433").split(":")
ENV_FILE = os.environ.get("HYPERCHAT_SMOKE_ENV_FILE", "/etc/hyperchat/api.env")
DSN = f"host=/var/run/postgresql port={DB_PORT} dbname={DB_NAME} user=hyperchat_app"
KNOWN_HEADS = ("0017", "0018", "0019", "0020", "0021")  # 0018 D6, 0019 D13, 0020/0021 KYC PR1/PR2
ORIGIN = {"origin": FRONT}
DOMAIN = "@authtest.invalid"
TAG = "authtest-sm" + secrets.token_hex(3)  # this run's creators: <TAG>a, <TAG>b, <TAG>c
SOCKET = "1234.5678"
STARTED = datetime.now(UTC)

results: list[tuple[str, str, str, str]] = []  # (section, name, PASS|FAIL|BLOCKED, detail)
secrets_seen: list[str] = []  # tokens and passwords that must never appear in a log


def check(section: str, name: str, ok: bool, detail: str = "") -> bool:
    results.append((section, name, "PASS" if ok else "FAIL", "" if ok else detail[:300]))
    print(f"{'PASS' if ok else 'FAIL'}  [{section}] {name}" + ("" if ok else f"  -- {detail[:300]}"), flush=True)
    return ok


def blocked(section: str, name: str, why: str) -> None:
    results.append((section, name, "BLOCKED", why))
    print(f"BLOCKED [{section}] {name}  -- {why}", flush=True)


def refuse(why: str) -> None:
    print(f"REFUSED: {why}", file=sys.stderr)
    sys.exit(2)


def db() -> psycopg.Connection:
    return psycopg.connect(DSN, autocommit=True)


def fixture_db() -> psycopg.Connection:
    """Synthetic creators and historical donations are written as the migrator (like the legacy import): the runtime
    role cannot create legacy-track creators and its only donation insert is checkout (migration 0020)."""
    conn = psycopg.connect(DSN.replace("user=hyperchat_app", "user=hyperchat_migrator"), autocommit=True)
    conn.execute("SET ROLE hyperchat_owner")
    return conn


def as_postgres(sql: str) -> str:
    out = subprocess.run(["runuser", "-u", "postgres", "--", "psql", "-X", "-At", "-p", DB_PORT, "-d", DB_NAME,
                          "-c", sql], capture_output=True, text=True, cwd="/")
    return (out.stdout or out.stderr).strip()


def password() -> str:
    pw = "Sm-" + secrets.token_urlsafe(18)
    secrets_seen.append(pw)
    return pw


def make_user(name: str, pw: str | None, role: str = "streamer", streamer_id: str | None = None,
              active: bool = True) -> str:
    with db() as c:
        return c.execute("INSERT INTO public.users (email, password_hash, role, streamer_id, is_active) "
                         "VALUES (%s, %s, %s, %s, %s) RETURNING id::text",
                         (f"{TAG}-{name}{DOMAIN}", hash_password(pw) if pw else None, role, streamer_id,
                          active)).fetchone()[0]


def email(name: str) -> str:
    return f"{TAG}-{name}{DOMAIN}"


def make_creator(suffix: str, owner: str | None) -> str:
    """An active legacy-track creator, as existing creators are after migration 0020."""
    with fixture_db() as c:
        return c.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, user_id, status, kyc_track, "
                         "kyc_baseline_owner_id) VALUES (%s, %s, %s, 'active', 'legacy', %s) RETURNING id::text",
                         (TAG + suffix, "Smoke " + suffix.upper(), owner, owner)).fetchone()[0]


def make_donation(streamer_id: str, donor: str, amount: int, moderation: str = "pending",
                  message: str | None = "Synthetic smoke message") -> str:
    with fixture_db() as c:
        return c.execute(
            "INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, currency, amount_inr, "
            "payment_status, moderation_status, message, message_visible) VALUES (%s, %s, %s, %s, 'INR', %s, "
            "'success', %s, %s, true) RETURNING id::text",
            (streamer_id, "authtest-" + secrets.token_hex(6), donor, amount, amount, moderation, message)).fetchone()[0]


def reset_login_counters() -> None:
    """The suite signs in many times from one IP (the VPS); clear the login counters it consumed (auth:login*)."""
    with db() as c:
        c.execute("DELETE FROM public.rate_limits WHERE endpoint LIKE 'auth:login%'")


class Session:
    """One signed-in browser: its own cookie jar, the CSRF header for writes."""

    def __init__(self, who: str, pw: str):
        self.client = httpx.Client(base_url=API, timeout=20)
        r = self.client.post("/api/v1/auth/login", json={"email": email(who), "password": pw}, headers=ORIGIN)
        self.status = r.status_code
        self.csrf = r.json().get("csrf_token", "") if r.status_code == 200 else ""
        self.headers = {**ORIGIN, "x-csrf-token": self.csrf}
        token = self.client.cookies.get("__Host-hc_session")
        secrets_seen.extend(t for t in (self.csrf, token) if t)

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def write(self, method, path, **kw):
        return self.client.request(method, path, headers={**self.headers, **kw.pop("headers", {})}, **kw)


def wait_for(predicate, seconds: float = 60, step: float = 1.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


# ── preconditions: DEV only, bots off ───────────────────────────────────────
if DB_NAME in ("hyperchat", "hyperchat_restore_test"):
    refuse("never against the production or restore database")
if "api.hyperchat.space" in API:
    refuse("never against the production API")
try:
    LOCAL_HEALTH = httpx.get(LOCAL + "/health", timeout=10).json()
except (httpx.HTTPError, ValueError) as exc:
    refuse(f"cannot read the local health endpoint ({type(exc).__name__})")
if LOCAL_HEALTH.get("environment") != "development":
    refuse(f"the API at {LOCAL} is not the development environment")
env_text = Path(ENV_FILE).read_text() if Path(ENV_FILE).exists() else ""
bots_flag = re.search(r"^HYPERCHAT_BOTS_ENABLED=(.*)$", env_text, re.M)
if bots_flag and bots_flag.group(1).strip().lower() in ("1", "true", "yes", "on"):
    refuse("bots are enabled on DEV; this suite only runs with bots disabled")
with db() as c:
    if c.execute("SELECT current_database()").fetchone()[0] != DB_NAME:
        refuse("connected to an unexpected database")

print(f"DEV backend smoke suite: run {TAG}, API {API}, release {LOCAL_HEALTH.get('release')}", flush=True)
ids: dict[str, str] = {}
sessions: list[Session] = []
try:
    # ── core / platform ─────────────────────────────────────────────────────
    S = "core"
    pub = httpx.get(API + "/health", timeout=15)
    check(S, "public /health: status only (F5)", pub.status_code == 200
          and pub.json() == {"status": "ok", "database": {"status": "ok"}}, pub.text)
    release = Path("/opt/hyperchat/current/backend/RELEASE").read_text().strip() \
        if Path("/opt/hyperchat/current/backend/RELEASE").exists() else ""
    check(S, "local /health: release, environment, database", LOCAL_HEALTH.get("status") == "ok"
          and LOCAL_HEALTH.get("release") == release and LOCAL_HEALTH.get("database", {}).get("status") == "ok",
          str(LOCAL_HEALTH))
    for unit in ("hyperchat-api", "hyperchat-worker"):
        state = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip()
        check(S, f"{unit} active", state == "active", state)
    head = as_postgres("SELECT version_num FROM alembic_version")
    check(S, f"database at a known migration head ({head})", head in KNOWN_HEADS, head)
    check(S, "bots disabled (HYPERCHAT_BOTS_ENABLED)", bool(bots_flag) and bots_flag.group(1).strip() == "false",
          bots_flag.group(1) if bots_flag else "missing")
    with db() as c:
        privs = c.execute("SELECT has_table_privilege(r, 'public.d1_hash_import_ledger', p) FROM (VALUES "
                          "('hyperchat_app','SELECT'),('hyperchat_app','INSERT'),('hyperchat_app','UPDATE'),"
                          "('hyperchat_app','DELETE'),('hyperchat_app','TRUNCATE'),('hyperchat_worker','SELECT'),"
                          "('hyperchat_worker','INSERT'),('hyperchat_readonly','SELECT'),"
                          "('hyperchat_readonly','INSERT')) v(r, p)").fetchall()
    check(S, "0017 ledger privileges: app SELECT+INSERT only, worker none, readonly SELECT only",
          [p[0] for p in privs] == [True, True, False, False, False, False, False, True, False], str(privs))
    with db() as c:
        cols = {r[0] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = "
                                        "'public' AND table_name = 'banned_donors'")}
    check(S, "0016 ban-audit columns present", {"unbanned_at", "unbanned_by_label", "unbanned_by_user_id"} <= cols,
          str(sorted(cols)))

    # ── synthetic fixtures ──────────────────────────────────────────────────
    reset_login_counters()
    pw = {k: password() for k in ("owner_a", "owner_b", "linked", "plain", "admin", "throttle", "change",
                                  "change_limit", "reset", "deact", "owner_c")}
    ids["owner_a"] = make_user("owner_a", pw["owner_a"])
    ids["owner_b"] = make_user("owner_b", pw["owner_b"])
    ids["a"] = make_creator("a", ids["owner_a"])
    ids["b"] = make_creator("b", ids["owner_b"])
    slug_a, slug_b = TAG + "a", TAG + "b"
    ids["linked"] = make_user("linked", pw["linked"], streamer_id=ids["a"])  # linked account = "moderator" access
    ids["plain"] = make_user("plain", pw["plain"], role="user")
    ids["admin"] = make_user("admin", pw["admin"], role="admin")
    ids["deact"] = make_user("deact", pw["deact"], active=False)
    for k in ("throttle", "change", "change_limit", "reset", "owner_c"):
        ids[k] = make_user(k, pw[k])

    # ── auth ────────────────────────────────────────────────────────────────
    S = "auth"
    owner_a = Session("owner_a", pw["owner_a"])
    sessions.append(owner_a)
    check(S, "login owner -> 200 with CSRF token", owner_a.status == 200 and len(owner_a.csrf) >= 32,
          str(owner_a.status))
    check(S, "session persists (GET /auth/session)", owner_a.get("/api/v1/auth/session").status_code == 200)
    r = owner_a.client.patch(f"/api/v1/dashboard/streamers/{slug_a}/settings", json={"tts_enabled": False},
                             headers=ORIGIN)
    check(S, "write without CSRF token -> 403", r.status_code == 403, r.text)
    r = httpx.post(API + "/api/v1/auth/login", json={"email": email("deact"), "password": pw["deact"]},
                   headers=ORIGIN, timeout=15)
    check(S, "deactivated account -> generic 401", r.status_code == 401 and r.json() == {"error": GENERIC}, r.text)

    # account-at-IP throttle (F2): 5 failures, then even the right password is refused, generically
    for _ in range(5):
        httpx.post(API + "/api/v1/auth/login", json={"email": email("throttle"), "password": "wrong-pass-123"},
                   headers=ORIGIN, timeout=15)
    r = httpx.post(API + "/api/v1/auth/login", json={"email": email("throttle"), "password": pw["throttle"]},
                   headers=ORIGIN, timeout=15)
    check(S, "account throttle: 6th attempt with the right password -> generic 401 (F2)",
          r.status_code == 401 and r.json() == {"error": GENERIC}, r.text)
    reset_login_counters()
    # IP throttle (F2/F9): 20 attempts from this IP, then a valid account is refused too
    for i in range(20):
        httpx.post(API + "/api/v1/auth/login", json={"email": f"{TAG}-nobody{i}{DOMAIN}", "password": "x"},
                   headers=ORIGIN, timeout=15)
    r = httpx.post(API + "/api/v1/auth/login", json={"email": email("throttle"), "password": pw["throttle"]},
                   headers=ORIGIN, timeout=15)
    check(S, "IP throttle: 21st attempt -> generic 401 (F2/F9)",
          r.status_code == 401 and r.json() == {"error": GENERIC}, r.text)
    reset_login_counters()
    r = httpx.post(API + "/api/v1/auth/login", json={"email": email("throttle"), "password": pw["throttle"]},
                   headers=ORIGIN, timeout=15)
    check(S, "after the window is cleared the account signs in again", r.status_code == 200, r.text)

    # password change: wrong current, success, other sessions revoked, current kept, old password dead (F3/F10)
    ch1, ch2 = Session("change", pw["change"]), Session("change", pw["change"])
    sessions += [ch1, ch2]
    with db() as c:  # a reset link issued before the change must die with it (F10)
        burned = secrets.token_urlsafe(32)
        secrets_seen.append(burned)
        c.execute("INSERT INTO public.password_reset_tokens (user_id, token_hash, expires_at) VALUES "
                  "(%s, %s, now() + interval '30 minutes')", (ids["change"], sha256_hex(burned)))
    r = ch1.write("POST", "/api/v1/auth/password/change", json={"current_password": "wrong-pass-123",
                                                                "new_password": "Unused-Pass-4242"})
    check(S, "password change with wrong current password -> 400", r.status_code == 400, r.text)
    new_pw = password()
    r = ch1.write("POST", "/api/v1/auth/password/change", json={"current_password": pw["change"],
                                                                "new_password": new_pw})
    check(S, "password change -> 2xx", r.is_success, r.text)
    check(S, "the changing session stays signed in", ch1.get("/api/v1/auth/session").status_code == 200)
    check(S, "other sessions are revoked by a password change", ch2.get("/api/v1/auth/session").status_code == 401)
    reset_login_counters()
    r = httpx.post(API + "/api/v1/auth/login", json={"email": email("change"), "password": pw["change"]},
                   headers=ORIGIN, timeout=15)
    check(S, "old password no longer signs in", r.status_code == 401, r.text)
    r = httpx.post(API + "/api/v1/auth/password/reset", json={"token": burned, "new_password": "Attacker-Pass-42"},
                   timeout=15)
    check(S, "a reset link issued before a password change is dead (F10)", r.status_code == 400, r.text)

    # password-change limit (F3): 5 verifications per user per hour, then 429 even for the right password
    cl = Session("change_limit", pw["change_limit"])
    sessions.append(cl)
    for _ in range(5):
        cl.write("POST", "/api/v1/auth/password/change", json={"current_password": "wrong-pass-123",
                                                               "new_password": "Unused-Pass-4242"})
    r = cl.write("POST", "/api/v1/auth/password/change", json={"current_password": pw["change_limit"],
                                                               "new_password": "Unused-Pass-4243"})
    check(S, "password-change limit: 6th attempt -> 429 (F3)", r.status_code == 429, r.text)

    # reset lifecycle with email disabled: the link is issued here (as the mailer would), used once, sessions revoked
    rs = Session("reset", pw["reset"])
    sessions.append(rs)
    link, expired = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    secrets_seen += [link, expired]
    with db() as c:
        c.execute("INSERT INTO public.password_reset_tokens (user_id, token_hash, expires_at) VALUES "
                  "(%s, %s, now() + interval '30 minutes'), (%s, %s, now() - interval '1 minute')",
                  (ids["reset"], sha256_hex(link), ids["reset"], sha256_hex(expired)))
    r = httpx.post(API + "/api/v1/auth/password/forgot", json={"email": email("reset")}, timeout=15)
    check(S, "forgot password with email disabled -> 202, nothing sent", r.status_code == 202, r.text)
    r = httpx.post(API + "/api/v1/auth/password/reset", json={"token": expired, "new_password": "Reset-Pass-4242"},
                   timeout=15)
    check(S, "expired reset link -> 400", r.status_code == 400, r.text)
    reset_pw = password()
    r = httpx.post(API + "/api/v1/auth/password/reset", json={"token": link, "new_password": reset_pw}, timeout=15)
    check(S, "reset with a valid link -> 2xx", r.is_success, r.text)
    check(S, "a reset revokes every session", rs.get("/api/v1/auth/session").status_code == 401)
    r = httpx.post(API + "/api/v1/auth/password/reset", json={"token": link, "new_password": "Again-Pass-4242"},
                   timeout=15)
    check(S, "a used reset link cannot be reused", r.status_code == 400, r.text)
    reset_login_counters()
    r = httpx.post(API + "/api/v1/auth/login", json={"email": email("reset"), "password": reset_pw},
                   headers=ORIGIN, timeout=15)
    check(S, "the new password signs in", r.status_code == 200, r.text)

    # ── admin, invites, onboarding ──────────────────────────────────────────
    S = "admin"
    admin = Session("admin", pw["admin"])
    sessions.append(admin)
    check(S, "admin signs in", admin.status == 200, str(admin.status))
    r = owner_a.get("/api/v1/admin/users")
    check(S, "non-admin -> 403 on /admin/users", r.status_code == 403, r.text)
    r = admin.get("/api/v1/admin/users", params={"limit": 5})
    check(S, "admin lists users", r.status_code == 200 and "users" in r.json(), r.text)
    r = admin.write("POST", "/api/v1/auth/invites", json={"email": email("invitee"), "role": "streamer"})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    leaked = [k for k, v in (body.get("invite", body) or {}).items() if isinstance(v, str) and "token=" in v]
    check(S, "invite created with email disabled; no accept link returned (links off on DEV)",
          r.status_code in (200, 201) and not leaked, r.text)
    r = admin.get("/api/v1/admin/invites")
    invites = [i for i in r.json().get("invites", []) if i.get("email") == email("invitee")]
    check(S, "admin lists the invite", r.status_code == 200 and len(invites) == 1, r.text)
    if invites:
        r = admin.write("POST", f"/api/v1/admin/invites/{invites[0]['id']}/revoke")
        check(S, "admin revokes the invite", r.is_success, r.text)
    # accept flow, the link issued here as the mailer would
    inv, inv_expired = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    secrets_seen += [inv, inv_expired]
    with db() as c:
        c.execute("INSERT INTO public.user_invites (email, role, token_hash, invited_by, expires_at) VALUES "
                  "(%s, 'streamer', %s, %s, now() + interval '1 day'), "
                  "(%s, 'streamer', %s, %s, now() - interval '1 minute')",
                  (email("accepted"), sha256_hex(inv), ids["admin"], email("late"), sha256_hex(inv_expired),
                   ids["admin"]))
    accept_pw = password()
    acc = httpx.Client(base_url=API, timeout=20)
    r = acc.post("/api/v1/auth/invites/accept", json={"token": inv, "password": accept_pw}, headers=ORIGIN)
    check(S, "invite accept -> signed in, account created",
          r.is_success and acc.get("/api/v1/auth/session").status_code == 200, r.text)
    acc.close()
    r = httpx.post(API + "/api/v1/auth/invites/accept", json={"token": inv, "password": accept_pw}, headers=ORIGIN,
                   timeout=15)
    check(S, "an accepted invite cannot be reused", r.status_code in (400, 409), r.text)
    r = httpx.post(API + "/api/v1/auth/invites/accept", json={"token": inv_expired, "password": accept_pw},
                   headers=ORIGIN, timeout=15)
    check(S, "expired invite -> 400", r.status_code == 400, r.text)

    r = admin.write("POST", "/api/v1/admin/creators", json={"slug": TAG + "c", "name": "Smoke C",
                                                             "razorpay_account": "primary"})
    created = r.json().get("creator", {}) if r.is_success else {}
    ids["c"] = created.get("id", "")
    check(S, "admin onboards a creator (CRE-2)", r.status_code in (200, 201) and bool(ids["c"]), r.text)
    r = admin.write("POST", "/api/v1/admin/creators", json={"slug": TAG + "c", "name": "Again",
                                                             "razorpay_account": "primary"})
    check(S, "duplicate creator slug -> 409", r.status_code == 409, r.text)
    r = owner_a.write("POST", "/api/v1/admin/creators", json={"slug": TAG + "x", "name": "X",
                                                               "razorpay_account": "primary"})
    check(S, "non-admin cannot onboard a creator", r.status_code == 403, r.text)
    if ids["c"]:
        r = admin.write("POST", f"/api/v1/admin/creators/{ids['c']}/owner", json={"user_id": ids["owner_c"]})
        check(S, "admin assigns the owner of an unclaimed creator", r.is_success, r.text)
        r = admin.write("POST", f"/api/v1/admin/creators/{ids['c']}/owner", json={"user_id": ids["plain"]})
        check(S, "a claimed creator cannot be re-assigned (D13 default)", r.status_code == 409, r.text)
    # ── creator lifecycle (KYC PR1, migration 0020): synthetic creators only ──
    S2 = "creator-lifecycle"
    if ids["c"] and admin.write("POST", f"/api/v1/admin/creators/{ids['c']}/activate", json={}).status_code == 404:
        blocked(S2, "Creator lifecycle (KYC PR1)", "not deployed on DEV yet")
    elif ids["c"]:
        check(S2, "a new creator is pending and hidden like an unknown one", created.get("status") == "pending"
              and httpx.get(f"{API}/api/v1/public/streamers/{TAG}c", timeout=15).status_code == 404)
        r = admin.write("POST", f"/api/v1/admin/creators/{ids['c']}/activate", json={"expected_status": "pending"})
        check(S2, "activation is refused until the owner's KYC exists (409)", r.status_code == 409, r.text)
        life = make_creator("life", ids["owner_c"])
        r = admin.write("POST", f"/api/v1/admin/creators/{life}/suspend", json={"expected_status": "active",
                                                                                "reason": "smoke"})
        check(S2, "suspend an existing creator (audited)", r.is_success and r.json()["creator"]["status"] == "suspended")
        check(S2, "a suspended creator is hidden like an unknown one",
              httpx.get(f"{API}/api/v1/public/streamers/{TAG}life", timeout=15).status_code == 404)
        r = admin.write("POST", f"/api/v1/admin/creators/{life}/reactivate", json={"expected_status": "suspended"})
        check(S2, "reactivate it (eligible again)", r.is_success and r.json()["donation_block"] is None, r.text)
    # deactivation revokes sessions; reactivation restores access
    victim = Session("plain", pw["plain"])
    sessions.append(victim)
    r = admin.write("PATCH", f"/api/v1/admin/users/{ids['plain']}", json={"is_active": False})
    check(S, "admin deactivates a user", r.is_success, r.text)
    check(S, "the deactivated user's session is revoked", victim.get("/api/v1/auth/session").status_code == 401)
    r = admin.write("PATCH", f"/api/v1/admin/users/{ids['plain']}", json={"is_active": True})
    check(S, "admin reactivates the user", r.is_success, r.text)
    reset_login_counters()
    plain = Session("plain", pw["plain"])
    sessions.append(plain)
    check(S, "the reactivated user signs in", plain.status == 200, str(plain.status))
    r = admin.write("POST", f"/api/v1/admin/users/{ids['plain']}/sessions/revoke")
    check(S, "admin revokes a user's sessions", r.is_success and plain.get("/api/v1/auth/session").status_code == 401,
          r.text)
    reset_login_counters()
    plain = Session("plain", pw["plain"])
    linked = Session("linked", pw["linked"])
    owner_b = Session("owner_b", pw["owner_b"])
    sessions += [plain, linked, owner_b]

    # ── authorization ───────────────────────────────────────────────────────
    S = "authz"
    settings_a = f"/api/v1/dashboard/streamers/{slug_a}/settings"
    for who, sess, want in (("owner", owner_a, 200), ("linked account", linked, 200), ("admin", admin, 200),
                            ("other creator's owner", owner_b, 403), ("ordinary user", plain, 403)):
        code = sess.get(settings_a).status_code
        check(S, f"{who} -> {want} on creator A's dashboard", code == want if want == 200 else code in (403, 404),
              str(code))
    check(S, "anonymous -> 401 on the dashboard", httpx.get(API + settings_a, timeout=15).status_code == 401)
    mine = owner_a.get("/api/v1/me/streamers").json()
    check(S, "/me/streamers lists the owned creator", slug_a in str(mine), str(mine))
    check(S, "/me/streamers of an ordinary user lists nothing",
          slug_a not in str(plain.get("/api/v1/me/streamers").json()))
    code = owner_b.get(f"/api/v1/streamers/{slug_a}/access").status_code
    check(S, "cross-creator access check refused", code in (403, 404), str(code))
    check(S, "linked account access check allowed", linked.get(f"/api/v1/streamers/{slug_a}/access").status_code == 200)

    # ── creator dashboard ───────────────────────────────────────────────────
    S = "dashboard"
    r = owner_a.write("PATCH", settings_a, json={"moderation_mode": "manual", "brand_color": "#123456",
                                                 "goal_is_active": True, "goal_name": "Smoke goal",
                                                 "goal_target_amount": 1000,
                                                 "goal_activated_at": (STARTED - timedelta(hours=1)).isoformat()})
    check(S, "settings update (moderation, colour, goal)", r.is_success, r.text)
    got = owner_a.get(settings_a).json()
    check(S, "settings read back", str(got.get("brand_color", "")).lower() == "#123456"
          and got.get("moderation_mode") == "manual", str(got)[:200])
    r = owner_a.write("PATCH", settings_a, json={"brand_color": "red"})
    check(S, "invalid settings value refused", r.status_code in (400, 422), r.text)
    d_ok = make_donation(ids["a"], "Smoke Supporter", 500)
    d_rej = make_donation(ids["a"], "Smoke Rejected", 50)
    d_ban = make_donation(ids["a"], "Smoke Spammer", 20)
    d_hide = make_donation(ids["a"], "Smoke Hidden", 30, moderation="auto_approved", message="hide me")
    d_b = make_donation(ids["b"], "Other Creator Donor", 40)
    base = f"/api/v1/dashboard/streamers/{slug_a}"
    q = owner_a.get(base + "/moderation/queue").json()
    check(S, "moderation queue lists the pending donations", all(d in str(q) for d in (d_ok, d_rej, d_ban)),
          str(q)[:200])
    r = owner_a.write("POST", f"{base}/donations/{d_ok}/moderation", json={"action": "approve"})
    check(S, "approve", r.is_success, r.text)
    r = owner_a.write("POST", f"{base}/donations/{d_rej}/moderation", json={"action": "reject"})
    check(S, "reject", r.is_success, r.text)
    r = linked.write("POST", f"{base}/donations/{d_hide}/moderation", json={"action": "hide_message"})
    check(S, "hide message (linked account)", r.is_success, r.text)
    r = linked.write("POST", f"{base}/donations/{d_hide}/moderation", json={"action": "unhide_message"})
    check(S, "unhide message", r.is_success, r.text)
    r = owner_b.write("POST", f"{base}/donations/{d_ok}/moderation", json={"action": "reject"})
    check(S, "other creator's owner cannot moderate", r.status_code in (403, 404), r.text)
    r = owner_a.write("POST", f"/api/v1/dashboard/streamers/{slug_a}/donations/{d_b}/moderation",
                      json={"action": "approve"})
    check(S, "a donation of another creator is not found here", r.status_code == 404, r.text)
    r = owner_a.write("POST", f"{base}/donations/{d_ban}/moderation", json={"action": "ban_donor",
                                                                            "reason": "smoke test"})
    check(S, "ban donor", r.is_success, r.text)
    bans = owner_a.get(base + "/moderation/banned-donors").json()
    ban = next((b for b in bans.get("banned_donors", []) if b.get("donor_name") == "Smoke Spammer"), None)
    check(S, "ban list shows the ban (DASH-4)", ban is not None and ban.get("is_active") is True, str(bans)[:200])
    if ban:
        r = owner_a.write("POST", f"{base}/moderation/banned-donors/{ban['ban_id']}/unban")
        check(S, "unban by id", r.is_success, r.text)
        with db() as c:
            row = c.execute("SELECT is_active, unbanned_at IS NOT NULL, unbanned_by_label, unbanned_by_user_id::text "
                            "FROM public.banned_donors WHERE id = %s", (ban["ban_id"],)).fetchone()
        check(S, "0016: the unban is recorded on the ban row", row is not None and row[0] is False and row[1]
              and row[2] and row[3] == ids["owner_a"], str(row))
        r = owner_b.write("POST",
                          f"/api/v1/dashboard/streamers/{slug_b}/moderation/banned-donors/{ban['ban_id']}/unban")
        check(S, "another creator cannot unban it", r.status_code == 404, r.text)
    r = owner_a.get(base + "/donations", params={"limit": 20})
    check(S, "donation history", r.status_code == 200 and d_ok in r.text, r.text[:200])
    r = owner_a.get(base + "/donations/stats")
    check(S, "donation stats",
          r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"), r.text[:200])
    today = (datetime.now(UTC) + timedelta(hours=5, minutes=30)).date().isoformat()  # export days are IST
    r = owner_a.get(base + "/donations/export.csv")
    check(S, "CSV export without a date range -> 400", r.status_code == 400, f"{r.status_code}")
    r = owner_a.get(base + "/donations/export.csv", params={"from": today, "to": today})
    check(S, "CSV export", r.status_code == 200 and "text/csv" in r.headers.get("content-type", "")
          and "Smoke Supporter" in r.text, r.headers.get("content-type", ""))
    r = owner_a.write("POST", f"{base}/moderators", json={"mod_name": "Smoke Mod", "telegram_user_id": "7000000001"})
    added = r.json() if r.is_success else {}
    mod_id = added.get("moderator_id") or added.get("moderator", {}).get("id") or added.get("id", "")
    check(S, "add moderator", r.status_code in (200, 201) and bool(mod_id), r.text)
    check(S, "list moderators", mod_id in owner_a.get(base + "/moderators").text)
    if mod_id:
        r = owner_a.write("PATCH", f"{base}/moderators/{mod_id}", json={"can_ban": True})
        check(S, "edit moderator permissions (DASH-12)",
              r.is_success and r.json().get("result") in ("applied", "unchanged"), r.text)
        r = owner_a.write("PATCH", f"{base}/moderators/{mod_id}", json={"can_ban": True})
        check(S, "same edit again -> unchanged", r.is_success and r.json().get("result") == "unchanged", r.text)
        r = owner_b.write("PATCH", f"/api/v1/dashboard/streamers/{slug_b}/moderators/{mod_id}", json={"can_ban": False})
        check(S, "another creator cannot edit it", r.status_code == 404, r.text)
        r = owner_a.write("DELETE", f"{base}/moderators/{mod_id}")
        check(S, "remove moderator", r.is_success, r.text)
    r = owner_a.get("/api/v1/me/terms")
    terms = r.json() if r.status_code == 200 else {}
    check(S, "terms status", r.status_code == 200 and bool(terms.get("current_version")), r.text)
    if terms.get("current_version"):
        r = owner_a.write("POST", "/api/v1/me/terms/accept", json={"version": terms["current_version"]})
        check(S, "terms accept", r.is_success and r.json().get("accepted") is True, r.text)
    r = owner_b.get(f"/api/v1/dashboard/streamers/{slug_b}/soundboard")
    check(S, "dashboard soundboard of a creator without it -> 403", r.status_code == 403, r.text[:200])
    # Custom Soundboard terms gate (D3): read-only soundboard checks; slot uploads would write R2 and stay BLOCKED.
    sb_terms = owner_a.get(base + "/soundboard/terms")
    if sb_terms.status_code == 404:  # the deployed release predates D3
        blocked(S, "Custom Soundboard terms gate (D3)", "not deployed on DEV yet; needs an approved DEV deploy")
        with db() as c:
            c.execute("UPDATE public.streamers SET soundboard_enabled = true WHERE id = %s", (ids["a"],))
        r = owner_a.get(base + "/soundboard")
        check(S, "dashboard soundboard read (pre-D3 release)", r.status_code == 200, r.text[:200])
    else:
        SB_VERSION = "soundboard-2026-10-13"
        st = sb_terms.json()
        check(S, "D3 status before eligibility: not eligible, not accepted, owner may accept",
              sb_terms.status_code == 200 and st.get("current_version") == SB_VERSION and st.get("eligible") is False
              and st.get("accepted") is False and st.get("usable") is False and st.get("can_accept") is True, str(st))
        sb_admin = f"/api/v1/admin/creators/{ids['a']}/soundboard"
        r = admin.write("PUT", sb_admin, json={"enabled": True})
        check(S, "D3 admin grants eligibility", r.status_code == 200 and r.json().get("changed") is True, r.text)
        check(S, "D3 non-admin cannot grant eligibility",
              owner_a.write("PUT", sb_admin, json={"enabled": True}).status_code == 403)
        st = owner_a.get(base + "/soundboard/terms").json()
        check(S, "D3 eligibility alone is not usable",
              st.get("eligible") is True and st.get("usable") is False, str(st))
        r = owner_a.get(base + "/soundboard")
        check(S, "D3 soundboard refused until the terms are accepted", r.status_code == 403
              and r.json().get("error") == "Accept the current Custom Soundboard terms first", r.text[:200])
        r = linked.write("POST", base + "/soundboard/terms/accept", json={"version": SB_VERSION})
        check(S, "D3 linked account cannot accept", r.status_code == 403, r.text)
        r = owner_a.write("POST", base + "/soundboard/terms/accept", json={"version": "soundboard-2026-07-14"})
        check(S, "D3 old version refused -> 409", r.status_code == 409, r.text)
        r = owner_a.write("POST", base + "/soundboard/terms/accept", json={"version": SB_VERSION})
        check(S, "D3 owner accepts the current version", r.status_code == 200 and r.json().get("usable") is True,
              r.text)
        r = owner_a.get(base + "/soundboard")
        check(S, "D3 soundboard usable once eligible and accepted", r.status_code == 200, r.text[:200])
        check(S, "D3 public soundboard served",
              httpx.get(f"{API}/api/v1/public/streamers/{slug_a}/soundboard", timeout=15).status_code == 200)
        r = admin.write("PUT", sb_admin, json={"enabled": False})
        check(S, "D3 admin revokes eligibility", r.status_code == 200 and r.json().get("changed") is True, r.text)
        r = owner_a.get(base + "/soundboard")
        check(S, "D3 revoked eligibility blocks the soundboard again", r.status_code == 403
              and r.json().get("error") == "Soundboard not enabled", r.text[:200])
        check(S, "D3 public soundboard no longer served",
              httpx.get(f"{API}/api/v1/public/streamers/{slug_a}/soundboard", timeout=15).status_code == 404)
        with db() as c:
            kept = c.execute("SELECT count(*) FROM public.soundboard_tnc_acceptances WHERE streamer_id = %s "
                             "AND terms_version = %s", (ids["a"], SB_VERSION)).fetchone()[0]
        check(S, "D3 revoking eligibility keeps the acceptance", kept == 1, str(kept))
        r = admin.write("PUT", sb_admin, json={"enabled": True})  # re-granted for the public checks below
        check(S, "D3 re-granted eligibility restores access with the kept acceptance",
              r.status_code == 200 and owner_a.get(base + "/soundboard").status_code == 200, r.text)
    r = owner_a.get(f"{base}/donations/{d_ok}/transcription")
    check(S, f"transcription status on a text donation answers {r.status_code} without a provider call",
          r.status_code in (200, 404, 409, 503), f"{r.status_code} {r.text[:150]}")

    # ── jobs / worker: the approval is processed (providers disabled -> silence fallback) ──
    S = "worker"
    def _audio_url(did: str):
        with db() as c:
            return c.execute("SELECT tts_audio_url FROM public.donations WHERE id = %s", (did,)).fetchone()[0]
    audio = wait_for(lambda: _audio_url(d_ok), 90)
    check(S, "worker processed the approval: audio URL set (providers off -> silence)", bool(audio), str(audio))
    with db() as c:
        job = c.execute("SELECT status, result->>'outcome', result->>'reason' FROM public.jobs WHERE kind = "
                        "'donation_audio' AND dedupe_key = %s ORDER BY created_at DESC LIMIT 1", (d_ok,)).fetchone()
    check(S, "donation_audio job succeeded with the silent outcome", job is not None and job[0] == "succeeded"
          and job[1] == "silent", str(job))

    # ── realtime ────────────────────────────────────────────────────────────
    S = "realtime"
    r = httpx.get(API + "/api/v1/realtime/config", params={"streamer": slug_a}, timeout=15)
    cfg = r.json() if r.status_code == 200 else {}
    check(S, "public config (key, cluster, auth endpoint)", r.status_code == 200 and {"key", "cluster"} <= set(cfg),
          r.text[:200])
    form = {"socket_id": SOCKET, "channel_name": f"private-{slug_a}-dashboard"}
    r = owner_a.client.post("/api/v1/realtime/pusher/auth", data=form, headers=owner_a.headers)
    check(S, "dashboard channel auth for the owner", r.status_code == 200 and ":" in r.json().get("auth", ""), r.text)
    r = owner_b.client.post("/api/v1/realtime/pusher/auth", data=form, headers=owner_b.headers)
    check(S, "dashboard channel refused for another creator", r.status_code == 403, r.text)
    r = owner_a.client.post("/api/v1/realtime/pusher/auth", data=form, headers=ORIGIN)
    check(S, "dashboard channel auth needs CSRF", r.status_code == 403, r.text)
    def _outbox_states(sid: str):
        with db() as c:
            rows = c.execute("SELECT status FROM public.realtime_outbox WHERE streamer_id = %s", (sid,)).fetchall()
        states = {r[0] for r in rows}
        return states if rows and states <= {"published", "skipped", "expired", "failed"} else None
    states = wait_for(lambda: _outbox_states(ids["a"]), 60, 2)
    check(S, "outbox events of the moderation actions were published by the worker (DEV Pusher)",
          states is not None and "published" in states and "failed" not in states, str(states))

    # ── OBS ─────────────────────────────────────────────────────────────────
    S = "obs"
    r = owner_a.write("POST", f"{base}/obs-tokens")
    tok_body = r.json() if r.status_code == 201 else {}
    media_url = tok_body.get("media_source_url", "")
    obs_token = parse_qs(urlsplit(media_url).query).get("token", [""])[0]
    secrets_seen.append(obs_token)
    token_id = tok_body.get("token", {}).get("id", "")
    check(S, "token created; shown once in the URLs; player URL keeps it in the #fragment",
          r.status_code == 201 and bool(obs_token) and "#token=" in tok_body.get("player_url", ""), r.text[:120])
    listing = owner_a.get(f"{base}/obs-tokens")
    check(S, "token list never contains the token", listing.status_code == 200 and token_id in listing.text
          and obs_token not in listing.text)
    hdr = {"x-obs-token": obs_token}
    check(S, "OBS session with the token",
          httpx.get(API + "/api/v1/obs/session", headers=hdr, timeout=15).status_code == 200)
    # Exposure cleanup: overlay settings and the leaderboard are OBS-token data for the TOKEN's creator.
    if httpx.get(API + "/api/v1/obs/overlay-config", timeout=15).status_code == 404:
        blocked(S, "OBS overlay-config / leaderboard (exposure cleanup)", "not deployed on DEV yet")
    else:
        for path in ("/api/v1/obs/overlay-config", "/api/v1/obs/leaderboard"):
            check(S, f"{path} without a token -> 401", httpx.get(API + path, timeout=15).status_code == 401)
            check(S, f"{path} with an invalid token -> 401", httpx.get(
                API + path, headers={"x-obs-token": "obs_" + "0" * 64}, timeout=15).status_code == 401)
        r = httpx.get(API + "/api/v1/obs/overlay-config", headers=hdr, params={"slug": slug_b}, timeout=15)
        check(S, "overlay-config: the token's creator, exactly the overlay settings",
              r.status_code == 200 and set(r.json()) == {
                  "brand_color", "alert_box_scale", "alert_overlay_id", "leaderboard_overlay_id", "goal_overlay_id",
                  "overlay_adjust_mode", "leaderboard_widget_enabled", "leaderboard_bg_enabled"}
              and r.headers.get("cache-control") == "no-store", r.text[:150])
        r = httpx.get(API + "/api/v1/obs/leaderboard", headers=hdr, timeout=15)
        check(S, "leaderboard: {enabled, top_donor} of the token's creator", r.status_code == 200
              and set(r.json()) == {"enabled", "top_donor"} and "latest" not in r.text, r.text[:150])
    r = httpx.post(API + "/api/v1/obs/audio/claim", headers=hdr, timeout=15)
    claim = r.json() if r.status_code == 200 else {}
    check(S, "claim returns the approved donation's audio", r.status_code == 200 and claim.get("donation_id") == d_ok,
          f"{r.status_code} {r.text[:150]}")
    r = httpx.post(API + "/api/v1/obs/audio/claim", headers=hdr, timeout=15)
    check(S, "queue empty afterwards -> 204", r.status_code == 204, str(r.status_code))
    r = owner_a.write("POST", f"{base}/donations/{d_ok}/replay")
    check(S, "replay", r.is_success, r.text)
    r = httpx.get(API + "/api/v1/obs/audio/next", params={"token": obs_token}, timeout=15, follow_redirects=False)
    check(S, "Media Source next -> 302 to the audio after a replay", r.status_code == 302
          and r.headers.get("location") == audio, f"{r.status_code}")
    r = httpx.get(API + "/api/v1/obs/audio/next", params={"token": obs_token}, timeout=15, follow_redirects=False)
    check(S, "next with an empty queue -> 204", r.status_code == 204, str(r.status_code))
    bad = httpx.get(API + "/api/v1/obs/session", headers={"x-obs-token": "obs_" + "0" * 64}, timeout=15)
    check(S, "invalid token -> 401", bad.status_code == 401, str(bad.status_code))
    form_alerts = {"socket_id": SOCKET, "channel_name": f"private-{slug_a}-alerts"}
    r = httpx.post(API + "/api/v1/realtime/pusher/auth", data=form_alerts, headers=hdr, timeout=15)
    check(S, "OBS token authorizes its own overlay channel", r.status_code == 200, r.text)
    r = httpx.post(API + "/api/v1/realtime/pusher/auth", data=form, headers=hdr, timeout=15)
    check(S, "OBS token never authorizes the dashboard channel", r.status_code == 403, r.text)
    r = httpx.post(API + "/api/v1/realtime/pusher/auth",
                   data={"socket_id": SOCKET, "channel_name": f"private-{slug_b}-alerts"}, headers=hdr, timeout=15)
    check(S, "OBS token refused for another creator's channel", r.status_code == 403, r.text)
    r = owner_a.client.post("/api/v1/realtime/pusher/auth", data=form_alerts, headers={**owner_a.headers, **hdr})
    check(S, "session and OBS token together -> 400", r.status_code == 400, r.text)
    player = httpx.get(API + "/obs/player", timeout=15)
    check(S, "player page served with a Content-Security-Policy", player.status_code == 200
          and "default-src 'none'" in player.headers.get("content-security-policy", ""))
    r = owner_a.write("POST", f"{base}/obs-tokens")  # a second token: rotation schedules the first for revocation
    second_body = r.json() if r.status_code == 201 else {}
    second = parse_qs(urlsplit(second_body.get("media_source_url", "")).query).get("token", [""])[0]
    secrets_seen.append(second)
    second_id = second_body.get("token", {}).get("id", "")
    if second_id:
        with db() as c:
            c.execute("UPDATE public.obs_tokens SET expires_at = now() - interval '1 minute' WHERE id = %s",
                      (second_id,))
        check(S, "expired token -> 401", httpx.get(API + "/api/v1/obs/session", headers={"x-obs-token": second},
                                                   timeout=15).status_code == 401)
    r = owner_a.write("DELETE", f"{base}/obs-tokens/{token_id}")
    check(S, "revoke token", r.is_success, r.text)
    check(S, "revoked token -> 401", httpx.get(API + "/api/v1/obs/session", headers=hdr, timeout=15).status_code == 401)
    r = owner_b.write("DELETE", f"/api/v1/dashboard/streamers/{slug_b}/obs-tokens/{token_id}")
    check(S, "another creator cannot revoke it", r.status_code == 404, r.text)

    # ── public API ──────────────────────────────────────────────────────────
    S = "public"
    for path in ("", "/pricing", "/goal", "/soundboard"):
        r = httpx.get(f"{API}/api/v1/public/streamers/{slug_a}{path}", timeout=15)
        check(S, f"GET /public/streamers/<slug>{path or ''}", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    old = {p: httpx.get(f"{API}/api/v1/public/streamers/{slug_a}{p}", timeout=15) for p in ("/overlay-config",
                                                                                             "/leaderboard")}
    if all(r.status_code == 200 for r in old.values()):
        blocked(S, "Anonymous creator data minimized (exposure cleanup)", "not deployed on DEV yet")
    else:
        for p, r in old.items():
            check(S, f"anonymous {p} is gone (no overlay settings, no donor data)",
                  r.status_code == 404 and "alert" not in r.text and "top_donor" not in r.text, str(r.status_code))
        profile = httpx.get(f"{API}/api/v1/public/streamers/{slug_a}", timeout=15).json()
        check(S, "profile: donor-page fields only (no leaderboard_widget_enabled)", set(profile) == {
            "slug", "name", "brand_color", "brand_logo_url", "background_url", "tts_enabled", "hyperemotes_enabled",
            "media_upload_enabled", "media_max_file_size_mb", "soundboard_enabled"}, str(sorted(profile)))
        g = httpx.get(f"{API}/api/v1/public/streamers/{slug_a}/goal", timeout=15).json()
        check(S, "goal: the public goal overlay's fields only (no goal_activated_at)", set(g) == {
            "active", "name", "target_amount_inr", "progress_inr", "brand_color", "goal_overlay_id"}, str(sorted(g)))
        pending = make_creator("pend", None)
        with db() as c:
            c.execute("UPDATE public.streamers SET status = 'pending' WHERE id = %s", (pending,))
            pslug = c.execute("SELECT streamer_slug FROM public.streamers WHERE id = %s", (pending,)).fetchone()[0]
        cfg = {ref: httpx.get(API + "/api/v1/realtime/config", params={"streamer": ref}, timeout=15)
               for ref in (pslug, TAG + "zz-unknown")}
        check(S, "realtime config: a pending creator looks exactly like an unknown one (404)",
              len({(r.status_code, r.text) for r in cfg.values()}) == 1
              and next(iter(cfg.values())).status_code == 404, str([r.status_code for r in cfg.values()]))
    goal = httpx.get(f"{API}/api/v1/public/streamers/{slug_a}/goal", timeout=15).json()
    check(S, "goal counts the approved donation", goal.get("active") is True and goal.get("progress_inr", 0) >= 500,
          str(goal)[:200])
    r = httpx.get(f"{API}/api/v1/public/streamers/{slug_b}/soundboard", timeout=15)
    check(S, "public soundboard of a creator without it -> 404", r.status_code == 404, str(r.status_code))
    check(S, "unknown creator -> 404",
          httpx.get(f"{API}/api/v1/public/streamers/{TAG}zz", timeout=15).status_code == 404)
    for path in ("/api/v1/public/hypersounds", "/api/v1/metrics/platform", "/api/v1/metrics/showcase"):
        r = httpx.get(API + path, timeout=15)
        check(S, f"GET {path}", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    signup = {"name": "Smoke Signup", "email": f"{TAG}-signup{DOMAIN}", "mobile_number": "+91 90000 00000"}
    a = httpx.post(API + "/api/v1/public/signups", json=signup, headers=ORIGIN, timeout=15)
    b = httpx.post(API + "/api/v1/public/signups", json=signup, headers=ORIGIN, timeout=15)
    check(S, "signup form: repeated email gets the identical answer (D14)", a.status_code == b.status_code == 201
          and a.json() == b.json(), f"{a.status_code} {b.status_code}")
    r = httpx.post(API + "/api/v1/public/signups", json=signup, headers={"origin": "https://evil.invalid"}, timeout=15)
    check(S, "signup form from a foreign origin -> 403", r.status_code == 403, r.text)
    # ── creator applications (KYC PR2, migration 0021): the synthetic signup above, synthetic creator only ──
    S3 = "applications"
    if admin.get("/api/v1/admin/applications").status_code == 404:
        blocked(S3, "Creator applications (KYC PR2)", "not deployed on DEV yet")
    else:
        with db() as c:
            app_id = c.execute("SELECT id::text FROM public.user_signups WHERE email = %s",
                               (f"{TAG}-signup{DOMAIN}",)).fetchone()[0]
        check(S3, "the signup form created a submitted application (no account)",
              admin.get(f"/api/v1/admin/applications/{app_id}").json()["application"]["review_status"] == "submitted")
        check(S3, "anonymous / non-admin cannot read applications",
              httpx.get(API + "/api/v1/admin/applications", timeout=15).status_code == 401
              and owner_a.get("/api/v1/admin/applications").status_code == 403)
        r = admin.write("POST", f"/api/v1/admin/applications/{app_id}/approve",
                        json={"slug": TAG + "app", "name": "Smoke App", "razorpay_account": "primary"})
        created_app = r.json().get("creator", {}) if r.is_success else {}
        check(S3, "approval creates a PENDING creator (never active), hidden publicly",
              created_app.get("status") == "pending"
              and httpx.get(f"{API}/api/v1/public/streamers/{TAG}app", timeout=15).status_code == 404, r.text[:200])
        r = admin.write("POST", f"/api/v1/admin/applications/{app_id}/approve",
                        json={"slug": TAG + "app2", "name": "Again", "razorpay_account": "primary"})
        check(S3, "a repeat approval is 409 and creates nothing", r.status_code == 409, r.text[:200])
        r = admin.write("POST", f"/api/v1/admin/applications/{app_id}/notes", json={"note": "smoke note"})
        check(S3, "internal note recorded in the history", r.is_success
              and [e["action"] for e in r.json()["events"]] == ["approve", "note"], r.text[:200])
    r = httpx.post(f"{API}/api/v1/public/streamers/{slug_a}/upload-grant", json={"kind": "voice"}, headers=ORIGIN,
                   timeout=15)
    grant = r.json() if r.status_code in (200, 201) else {}
    check(S, "upload grant issued (no upload, no R2 write)", r.status_code in (200, 201) and bool(grant), r.text[:150])
    secrets_seen.extend(v for v in grant.values() if isinstance(v, str) and len(v) > 20)
    r = httpx.post(f"{API}/api/v1/public/streamers/{slug_a}/upload-grant", json={"kind": "voice"},
                   headers={"origin": "https://evil.invalid"}, timeout=15)
    check(S, "upload grant from a foreign origin -> 403", r.status_code == 403, r.text)

    # ── payments without external calls ─────────────────────────────────────
    S = "payments"
    with db() as c:
        order = c.execute("SELECT order_id FROM public.donations WHERE id = %s", (d_ok,)).fetchone()[0]
    a = httpx.get(f"{API}/api/v1/public/donations/{order}/status", headers={"x-status-token": "0" * 43}, timeout=15)
    b = httpx.get(f"{API}/api/v1/public/donations/authtest-nonexistent/status", headers={"x-status-token": "0" * 43},
                  timeout=15)
    check(S, "status: wrong token and unknown order give the same 404", a.status_code == b.status_code == 404
          and a.json() == b.json(), f"{a.status_code} {b.status_code}")
    r = httpx.post(f"{API}/api/v1/public/donations/{order}/confirm", headers={**ORIGIN, "x-status-token": "0" * 43},
                   json={"razorpay_payment_id": "pay_authtest", "razorpay_order_id": order,
                         "razorpay_signature": "0" * 64}, timeout=15)
    check(S, "confirm with a wrong status token -> 4xx before any provider call", 400 <= r.status_code < 500,
          f"{r.status_code} {r.text[:120]}")
    r = httpx.post(API + "/api/v1/webhooks/razorpay", content=b'{"event":"payment.captured"}',
                   headers={"x-razorpay-signature": "0" * 64, "content-type": "application/json"}, timeout=15)
    check(S, "webhook with a bad signature refused", 400 <= r.status_code < 500, f"{r.status_code} {r.text[:120]}")

    # ── partner API (D4, ClipWave): a synthetic DEV-only key; the real ClipWave keys are never read ──
    S = "partner"
    PARTNER = TAG + "-partner"  # "authtest-..." so the cleanup removes it, its signups and its rate-limit rows
    probe = httpx.post(API + "/api/v1/partner/streamers/check", json={"channel_name": slug_a}, timeout=15)
    if probe.status_code == 404:  # the deployed release predates D4
        blocked(S, "ClipWave partner API (D4)", "not deployed on DEV yet; needs an approved DEV deploy")
    else:
        pkey = f"hc_{PARTNER}_{secrets.token_hex(32)}"
        secrets_seen.append(pkey)
        with db() as c:
            c.execute("INSERT INTO public.partner_api_keys (partner_slug, key_hash, key_prefix) VALUES (%s, %s, %s)",
                      (PARTNER, sha256_hex(pkey), pkey[:12]))
        good = {"x-api-key": pkey}
        check_url, signup_url = API + "/api/v1/partner/streamers/check", API + "/api/v1/partner/signups"
        check(S, "D4 missing API key -> 401", probe.status_code == 401
              and probe.json() == {"error": "Invalid or missing API key"}, probe.text)
        r = httpx.post(check_url, json={"channel_name": slug_a}, headers={"x-api-key": "hc_x_" + "0" * 64}, timeout=15)
        check(S, "D4 unknown API key -> 401", r.status_code == 401, r.text)
        r = httpx.post(check_url, json={"channel_name": slug_a.upper()}, headers=good, timeout=15)
        check(S, "D4 creator check: existing synthetic creator -> exists, nothing else",
              r.status_code == 200 and r.json() == {"success": True, "exists": True,
                                                    "message": "This creator is already registered on HyperChat."},
              r.text)
        r = httpx.post(check_url, json={"channel_name": TAG + "zz-missing"}, headers=good, timeout=15)
        check(S, "D4 creator check: missing creator -> not exists", r.status_code == 200
              and r.json().get("exists") is False, r.text)
        signup = {"channel_name": "Smoke Partner Creator", "mobile_number": "9000000000",
                  "youtube_channel": "https://youtube.com/@smoke-partner", "email": f"{TAG}-partner{DOMAIN}"}
        r = httpx.post(signup_url, json={**signup, "partner_slug": "clipwave"}, headers=good, timeout=15)
        check(S, "D4 partner_slug cannot be supplied (spoofing refused)", r.status_code == 400
              and "partner_slug" in r.json().get("fields", {}), r.text)
        r = httpx.post(signup_url, json=signup, headers={**good, "user-agent": "hyperchat-smoke"}, timeout=15)
        check(S, "D4 signup recorded -> 201", r.status_code == 201 and r.json().get("success") is True, r.text)
        r = httpx.post(signup_url, json={**signup, "email": signup["email"].upper()}, headers=good, timeout=15)
        check(S, "D4 duplicate signup -> 200 duplicate", r.status_code == 200 and r.json().get("duplicate") is True,
              r.text)
        with db() as c:
            rows = c.execute("SELECT partner_slug, count(*) FROM public.partner_signups WHERE email = %s "
                             "GROUP BY partner_slug", (signup["email"],)).fetchall()
            users = c.execute("SELECT count(*) FROM public.users WHERE email::text = %s",
                              (signup["email"],)).fetchone()[0]
            c.execute("UPDATE public.partner_api_keys SET revoked = true WHERE key_hash = %s", (sha256_hex(pkey),))
        check(S, "D4 one signup stored under the key's partner; no user created", rows == [(PARTNER, 1)]
              and users == 0, str(rows))
        r = httpx.post(check_url, json={"channel_name": slug_a}, headers=good, timeout=15)
        check(S, "D4 revoked key -> 401", r.status_code == 401, r.text)

    # ── payments account model (D6): one 'primary' account; no Razorpay call, no order ──
    S = "payments-account"
    if head not in ("0018", "0019"):  # the deployed release predates D6
        blocked(S, "Razorpay secondary retired (D6)", "not deployed on DEV yet; needs an approved DEV deploy")
    else:
        with db() as c:
            orders_before = c.execute("SELECT count(*) FROM public.donations WHERE provider_order_id IS NOT NULL "
                                      "AND created_at >= %s", (STARTED,)).fetchone()[0]
            ankit = c.execute("SELECT razorpay_account FROM public.streamers WHERE streamer_slug = 'ankit'").fetchone()
            defs = dict(c.execute("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conname IN "
                                  "('streamers_razorpay_account_check', 'donations_payment_account_check')").fetchall())
            accounts = c.execute("SELECT DISTINCT razorpay_account FROM public.streamers "
                                 "WHERE razorpay_account IS NOT NULL").fetchall()
        check(S, "D6 both account constraints allow only 'primary'", len(defs) == 2
              and all("'primary'" in d and "secondary" not in d for d in defs.values()), str(defs))
        check(S, "D6 no creator is on another account", accounts == [("primary",)], str(accounts))
        check(S, "D6 the ankit testing page is on primary (DEV primary = test mode)",
              ankit is not None and ankit[0] == "primary", str(ankit))
        r = admin.write("POST", "/api/v1/admin/creators", json={"slug": TAG + "d6", "name": "Smoke D6",
                                                                "razorpay_account": "secondary"})
        check(S, "D6 onboarding refuses 'secondary' -> 422", r.status_code == 422, r.text[:200])
        r = admin.write("POST", "/api/v1/admin/creators", json={"slug": TAG + "d6", "name": "Smoke D6",
                                                                "razorpay_account": "primary"})
        check(S, "D6 onboarding with the configured primary account -> 201", r.status_code == 201
              and r.json().get("creator", {}).get("razorpay_account") == "primary", r.text[:200])
        with db() as c:
            orders_after = c.execute("SELECT count(*) FROM public.donations WHERE provider_order_id IS NOT NULL "
                                     "AND created_at >= %s", (STARTED,)).fetchone()[0]
        check(S, "D6 no Razorpay order was created during this run", orders_after == orders_before == 0,
              f"{orders_before} -> {orders_after}")

    # ── payment health (D7): manual check only; the probe itself is never run by the smoke ──
    S = "payment-health"
    anon = httpx.get(API + "/api/v1/admin/payments/health", timeout=15)
    if anon.status_code == 404:  # the deployed release predates D7
        blocked(S, "Admin/manual payment-health check (D7)", "not deployed on DEV yet; needs an approved DEV deploy")
    else:
        check(S, "D7 health endpoint needs a session -> 401", anon.status_code == 401, str(anon.status_code))
        r = owner_a.get("/api/v1/admin/payments/health")
        check(S, "D7 health endpoint refused for a non-admin -> 403", r.status_code == 403, str(r.status_code))
        cli_help = subprocess.run(["/opt/hyperchat/current/venv/bin/python", "-m", "app.cli", "--help"],
                                  capture_output=True, text=True, cwd="/opt/hyperchat/current/backend")
        check(S, "D7 CLI command payment-health is installed (help only, no probe)",
              cli_help.returncode == 0 and "payment-health" in cli_help.stdout, cli_help.stderr[-200:])
        blocked(S, "D7 admin health result and CLI probe",
                "BLOCKED_BY_APPROVAL: both call Razorpay (GET /payments?count=1, test mode on DEV)")

    # ── admin email correction (D11): synthetic users only, no email sent ───
    S = "admin-email"
    anon = httpx.put(f"{API}/api/v1/admin/users/{ids['plain']}/email", json={"email": email("d11x")},
                     headers=ORIGIN, timeout=15)
    if anon.status_code in (404, 405):  # the deployed release predates D11
        blocked(S, "Admin email correction (D11)", "not deployed on DEV yet; needs an approved DEV deploy")
    else:
        check(S, "D11 needs a session -> 401", anon.status_code == 401, str(anon.status_code))
        d11_pw = password()
        ids["d11"] = make_user("d11old", d11_pw)
        make_user("d11taken", None)
        old_reset = secrets.token_urlsafe(32)
        secrets_seen.append(old_reset)
        with db() as c:  # verified email, an open reset link, a linked Google identity, a pending invite elsewhere
            c.execute("UPDATE public.users SET email_verified_at = now() WHERE id = %s", (ids["d11"],))
            c.execute("INSERT INTO public.password_reset_tokens (user_id, token_hash, expires_at) VALUES "
                      "(%s, %s, now() + interval '30 minutes')", (ids["d11"], sha256_hex(old_reset)))
            c.execute("INSERT INTO public.user_identities (user_id, provider, provider_subject) "
                      "VALUES (%s, 'google', %s)", (ids["d11"], TAG + "-sub"))
            c.execute("INSERT INTO public.user_invites (email, token_hash, expires_at) VALUES "
                      "(%s, %s, now() + interval '1 day')", (email("d11inv"), sha256_hex(secrets.token_urlsafe(32))))
        d11 = Session("d11old", d11_pw)
        sessions.append(d11)
        url = f"/api/v1/admin/users/{ids['d11']}/email"
        r = owner_a.write("PUT", url, json={"email": email("d11new")})
        check(S, "D11 non-admin -> 403", r.status_code == 403, r.text[:200])
        r = admin.write("PUT", f"/api/v1/admin/users/{ids['admin']}/email", json={"email": email("d11self")})
        check(S, "D11 admin changing their own email -> 403", r.status_code == 403, r.text[:200])
        r = admin.write("PUT", url, json={"email": email("d11taken").upper()})
        check(S, "D11 address of another account (any case) -> 409", r.status_code == 409, r.text[:200])
        r = admin.write("PUT", url, json={"email": email("d11inv")})
        check(S, "D11 address with a pending invite -> 409", r.status_code == 409, r.text[:200])
        r = admin.write("PUT", url, json={"email": email("d11new"), "role": "admin"})
        check(S, "D11 unknown body field -> 422", r.status_code == 422, r.text[:200])
        check(S, "D11 refusals changed nothing (old session still valid)",
              d11.get("/api/v1/auth/session").status_code == 200)
        r = admin.write("PUT", url, json={"email": "  " + email("d11new").upper() + " "})
        body = r.json() if r.status_code == 200 else {}
        check(S, "D11 change -> 200 with counts (1 session, 1 reset link, 1 Google link)",
              body.get("changed") is True and body.get("user", {}).get("email") == email("d11new")
              and (body.get("sessions_revoked"), body.get("reset_tokens_burned"), body.get("identities_removed"))
              == (1, 1, 1), r.text[:300])
        with db() as c:
            row = c.execute("SELECT email::text, email_verified_at IS NULL, (SELECT count(*) FROM "
                            "public.user_identities WHERE user_id = %s) FROM public.users WHERE id = %s",
                            (ids["d11"], ids["d11"])).fetchone()
        check(S, "D11 stored lowercase, unverified, Google link removed", row == (email("d11new"), True, 0), str(row))
        check(S, "D11 the old session is revoked", d11.get("/api/v1/auth/session").status_code == 401)
        r = httpx.post(API + "/api/v1/auth/password/reset", json={"token": old_reset, "new_password": "Taken-Pass-42"},
                       timeout=15)
        check(S, "D11 the old reset link is dead", r.status_code == 400, r.text[:200])
        reset_login_counters()
        r = httpx.post(API + "/api/v1/auth/login", json={"email": email("d11old"), "password": d11_pw},
                       headers=ORIGIN, timeout=15)
        check(S, "D11 the old email no longer signs in", r.status_code == 401, r.text[:200])
        d11_new = Session("d11new", d11_pw)
        sessions.append(d11_new)
        check(S, "D11 the new email signs in with the unchanged password", d11_new.status == 200, str(d11_new.status))
        r = admin.write("PUT", url, json={"email": email("d11new").upper()})
        check(S, "D11 same address ignoring case -> 200 changed:false, nothing revoked",
              r.status_code == 200 and r.json().get("changed") is False and r.json().get("sessions_revoked") == 0
              and d11_new.get("/api/v1/auth/session").status_code == 200, r.text[:200])
        reset_login_counters()

    # ── creator ownership transfer (D13): synthetic users and creator only, no external call ──
    S = "owner-transfer"
    anon = httpx.post(f"{API}/api/v1/admin/creators/{ids['a']}/owner/transfer", headers=ORIGIN, timeout=15,
                      json={"user_id": ids["plain"], "expected_owner_id": ids["owner_a"]})
    if anon.status_code in (404, 405):  # the deployed release predates D13
        blocked(S, "Admin creator ownership transfer (D13)", "not deployed on DEV yet; needs an approved DEV deploy")
    else:
        check(S, "D13 needs a session -> 401", anon.status_code == 401, str(anon.status_code))
        check(S, "D13 database at migration 0019 (owner-specific soundboard terms)", head == "0019", head)
        d13_pw = {k: password() for k in ("d13old", "d13new")}
        ids["d13old"] = make_user("d13old", d13_pw["d13old"])
        ids["d13new"] = make_user("d13new", d13_pw["d13new"], role="user")
        ids["d13off"] = make_user("d13off", None, active=False)
        ids["d13"] = make_creator("d13", ids["d13old"])
        slug_d13 = TAG + "d13"
        with db() as c:  # migrated shape: owner linked both ways; eligible and accepted; history and an OBS token
            c.execute("UPDATE public.users SET streamer_id = %s WHERE id = %s", (ids["d13"], ids["d13old"]))
            c.execute("UPDATE public.streamers SET soundboard_enabled = true, razorpay_account = 'primary' "
                      "WHERE id = %s", (ids["d13"],))
            c.execute("INSERT INTO public.soundboard_tnc_acceptances (streamer_slug, streamer_id, user_id, "
                      "terms_version) VALUES (%s, %s, %s, 'soundboard-2026-10-13')",
                      (slug_d13, ids["d13"], ids["d13old"]))
            c.execute("INSERT INTO public.obs_tokens (streamer_id, token_hash) VALUES (%s, %s)",
                      (ids["d13"], sha256_hex(secrets.token_urlsafe(32))))
        make_donation(ids["d13"], "D13 donor", 150)

        def d13_snapshot() -> str:
            with db() as c:
                return str(c.execute("""
                    SELECT (SELECT to_jsonb(s) - 'user_id' - 'updated_at' FROM public.streamers s WHERE s.id = %(s)s),
                           (SELECT jsonb_agg(to_jsonb(d) ORDER BY d.id) FROM public.donations d
                            WHERE d.streamer_id = %(s)s),
                           (SELECT jsonb_agg(to_jsonb(o) ORDER BY o.id) FROM public.obs_tokens o
                            WHERE o.streamer_id = %(s)s),
                           (SELECT jsonb_agg(to_jsonb(t) ORDER BY t.id) FROM public.soundboard_tnc_acceptances t
                            WHERE t.streamer_id = %(s)s)""", {"s": ids["d13"]}).fetchone())

        def d13_account(key: str) -> tuple:
            with db() as c:
                return c.execute("SELECT email::text, role, is_active, streamer_id::text FROM public.users "
                                 "WHERE id = %s", (ids[key],)).fetchone()

        d13_old, d13_new = Session("d13old", d13_pw["d13old"]), Session("d13new", d13_pw["d13new"])
        sessions += [d13_old, d13_new]
        access = f"/api/v1/streamers/{slug_d13}/access"
        public_sb = f"{API}/api/v1/public/streamers/{slug_d13}"
        check(S, "D13 before: old owner has access, new owner does not",
              d13_old.get(access).status_code == 200 and d13_new.get(access).status_code == 403)
        check(S, "D13 before: soundboard usable (eligible + the owner's acceptance)",
              httpx.get(public_sb, timeout=15).json().get("soundboard_enabled") is True)
        url = f"/api/v1/admin/creators/{ids['d13']}/owner/transfer"
        r = owner_a.write("POST", url, json={"user_id": ids["d13new"], "expected_owner_id": ids["d13old"]})
        check(S, "D13 non-admin -> 403", r.status_code == 403, r.text[:200])
        r = admin.write("POST", url, json={"user_id": ids["d13new"], "expected_owner_id": ids["d13new"]})
        check(S, "D13 stale expected_owner_id -> 409", r.status_code == 409, r.text[:200])
        r = admin.write("POST", url, json={"user_id": ids["d13off"], "expected_owner_id": ids["d13old"]})
        check(S, "D13 inactive target -> 400", r.status_code == 400, r.text[:200])
        check(S, "D13 refusals changed nothing (old owner still has access)", d13_old.get(access).status_code == 200)
        before, new_before = d13_snapshot(), d13_account("d13new")
        r = admin.write("POST", url, json={"user_id": ids["d13new"], "expected_owner_id": ids["d13old"]})
        body = r.json() if r.status_code == 200 else {}
        check(S, "D13 transfer -> 200 (previous owner, 1 session revoked, unlinked, 1 active OBS token)",
              body.get("changed") is True and body.get("previous_owner_id") == ids["d13old"]
              and body.get("sessions_revoked") == 1 and body.get("old_owner_unlinked") is True
              and body.get("active_obs_tokens") == 1 and "@" not in r.text, r.text[:300])
        check(S, "D13 the old owner's session is revoked", d13_old.get("/api/v1/auth/session").status_code == 401)
        check(S, "D13 the old owner's link to this creator is cleared", d13_account("d13old")[3] is None)
        reset_login_counters()
        d13_old2 = Session("d13old", d13_pw["d13old"])
        sessions.append(d13_old2)
        check(S, "D13 the old owner signs in but has no access to the creator",
              d13_old2.status == 200 and d13_old2.get(access).status_code == 403)
        check(S, "D13 the new owner has access on the existing session", d13_new.get(access).status_code == 200)
        check(S, "D13 the new owner's account is untouched", d13_account("d13new") == new_before)
        check(S, "D13 creator, donations, OBS tokens and acceptances unchanged", d13_snapshot() == before)
        st = d13_new.get(f"/api/v1/dashboard/streamers/{slug_d13}/soundboard/terms").json()
        check(S, "D13 soundboard unusable until the new owner accepts",
              st.get("eligible") is True and st.get("usable") is False and st.get("can_accept") is True
              and httpx.get(public_sb, timeout=15).json().get("soundboard_enabled") is False, str(st))
        r = d13_new.write("POST", f"/api/v1/dashboard/streamers/{slug_d13}/soundboard/terms/accept",
                          json={"version": "soundboard-2026-10-13"})
        check(S, "D13 the new owner accepts and the soundboard is usable again",
              r.status_code == 200 and r.json().get("usable") is True
              and httpx.get(public_sb, timeout=15).json().get("soundboard_enabled") is True, r.text[:200])
        r = admin.write("POST", url, json={"user_id": ids["d13new"], "expected_owner_id": ids["d13new"]})
        check(S, "D13 transfer to the current owner -> 200 changed:false",
              r.status_code == 200 and r.json().get("changed") is False and r.json().get("sessions_revoked") == 0,
              r.text[:200])
        reset_login_counters()

    # ── abandoned-upload cleanup (D9): synthetic objects in the DEV bucket only, approval-gated ──
    S = "upload-cleanup"
    deployed = Path("/opt/hyperchat/current/backend/app/maintenance.py")
    if not (deployed.exists() and "def abandoned_uploads" in deployed.read_text()):
        blocked(S, "Abandoned-upload cleanup (D9)", "not deployed on DEV yet; needs an approved DEV deploy")
    elif os.environ.get("HYPERCHAT_SMOKE_R2") != "1":
        blocked(S, "Abandoned-upload cleanup (D9)",
                "BLOCKED_BY_APPROVAL: writes and deletes synthetic objects in the DEV R2 bucket (HYPERCHAT_SMOKE_R2=1)")
    else:
        import asyncio
        import uuid

        from app.cli import connection_factory
        from app.core.config import Settings
        from app.maintenance import abandoned_uploads
        from app.storage.r2 import build_object_store

        settings = Settings(_env_file=ENV_FILE)
        r2 = build_object_store(settings)
        slug_d9 = TAG + "d9"
        now = time.time()

        def up_key(prefix: str, ms: float, ext: str) -> str:
            return f"{prefix}/{slug_d9}_{int(ms * 1000)}_{secrets.token_hex(16)}.{ext}"

        orphan_voice = up_key("voice-messages", now - 25 * 3600, "webm")
        orphan_media = up_key("donation-media", now - 25 * 3600, "png")
        referenced = up_key("voice-messages", now - 25 * 3600, "webm")
        too_new = up_key("donation-media", now + 2 * 3600, "png")  # still young under the simulated clock below
        board = f"soundboards/{slug_d9}/{uuid.uuid4()}.mp3"
        mine = {orphan_voice, orphan_media, referenced, too_new}

        class OnlyThisRun:
            """The deployed cleanup, but it sees (and may delete) only this run's synthetic keys."""
            def public_url(self, k):
                return r2.public_url(k)

            async def list_uploads(self, prefix):
                return [o for o in await r2.list_uploads(prefix) if o.key in mine]

            async def delete_upload(self, k):
                assert k in mine, "refusing to delete a key this run did not create"
                await r2.delete_upload(k)

        async def d9() -> tuple:
            for k, t in ((orphan_voice, "audio/webm"), (orphan_media, "image/png"), (referenced, "audio/webm"),
                         (too_new, "image/png"), (board, "audio/mpeg")):
                await r2.put_object(k, b"synthetic", content_type=t)
            sid = make_creator("d9", None)
            make_donation(sid, "D9 donor", 50)
            with db() as c:
                c.execute("UPDATE public.donations SET voice_message_url = %s WHERE streamer_id = %s",
                          (r2.public_url(referenced), sid))
            later = datetime.fromtimestamp(now + 25 * 3600, UTC)  # "a day later": the synthetic orphans are old
            dry = await abandoned_uploads(OnlyThisRun(), connection_factory(settings), dry_run=True, now=later)
            done = await abandoned_uploads(OnlyThisRun(), connection_factory(settings), dry_run=False, now=later)
            present = {k: await r2.head_upload(k) is not None for k in mine}
            board_kept = await r2.head_object(board) is not None
            for k in (referenced, too_new):  # remove what the cleanup rightly kept
                await r2.delete_upload(k)
            await r2.delete_object(board)
            left = [k for k in mine if await r2.head_upload(k) is not None] + (
                [board] if await r2.head_object(board) is not None else [])
            return dry, done, present, board_kept, left

        try:
            dry, done, present, board_kept, left = asyncio.run(d9())
        except Exception as exc:  # noqa: BLE001 - the reason class only
            check(S, "D9 cleanup run", False, type(exc).__name__)
        else:
            check(S, "D9 dry run: 2 eligible, 1 referenced, 1 too new, nothing deleted",
                  (dry["eligible"], dry["kept_referenced"], dry["kept_too_new"], dry["deleted"]) == (2, 1, 1, 0),
                  str(dry))
            check(S, "D9 old unreferenced voice and media deleted",
                  done["deleted"] == 2 and not present[orphan_voice] and not present[orphan_media], str(done))
            check(S, "D9 referenced and too-new uploads kept", present[referenced] and present[too_new], str(present))
            check(S, "D9 soundboard object never touched", board_kept)
            check(S, "D9 synthetic objects removed afterwards", left == [], str(len(left)))

    # ── public contact config (D5): anonymous read of the HYPERCHAT_PUBLIC_* values; nothing written ──
    S = "public-config"
    r = httpx.get(API + "/api/v1/public/config", timeout=15)
    if r.status_code == 404:
        blocked(S, "Public contact config (D5)", "not deployed on DEV yet; needs an approved DEV deploy")
    else:
        from pydantic import SecretStr

        from app.core.config import Settings

        cfg = Settings(_env_file=ENV_FILE)
        body = r.json() if r.status_code == 200 else {}
        contact = body.get("contact") if isinstance(body, dict) else None
        check(S, "D5 GET /api/v1/public/config anonymously -> 200, cacheable, no cookie",
              r.status_code == 200 and r.headers.get("cache-control") == "public, max-age=300"
              and "set-cookie" not in r.headers, f"{r.status_code} {r.headers.get('cache-control')}")
        check(S, "D5 only the allow-listed fields", set(body) == {"contact"} and isinstance(contact, dict)
              and set(contact) == {"name", "email", "phones", "discord_url"}, str(sorted(body))[:120])
        expected = {"name": cfg.public_contact_name or None, "email": cfg.public_contact_email or None,
                    "phones": [x for x in cfg.public_contact_phones.split(",") if x],
                    "discord_url": cfg.public_discord_url or None}
        check(S, "D5 values match the DEV configuration (null / [] when unset)", contact == expected,
              "configured: " + (", ".join(k for k, v in expected.items() if v) or "none"))
        secret_values = [v.get_secret_value() for v in (getattr(cfg, f) for f in type(cfg).model_fields)
                         if isinstance(v, SecretStr) and len(v.get_secret_value()) >= 8]
        check(S, "D5 no configured secret in the response", not [v for v in secret_values if v in r.text])
        check(S, "D5 read-only: POST -> 405",
              httpx.post(API + "/api/v1/public/config", json={}, headers=ORIGIN, timeout=15).status_code == 405)
        check(S, "D5 no contact-form submission route", httpx.post(API + "/api/v1/public/contact", json={},
                                                                  headers=ORIGIN, timeout=15).status_code == 404)

    # ── bots: disabled ──────────────────────────────────────────────────────
    S = "bots"
    for hook in ("telegram", "discord"):
        r = httpx.post(f"{API}/api/v1/webhooks/{hook}", json={}, timeout=15)
        check(S, f"{hook} webhook answers 503 while bots are disabled", r.status_code == 503, f"{r.status_code}")
    with db() as c:
        sent = c.execute("SELECT count(*) FROM public.moderator_notifications WHERE streamer_id = ANY(%s::uuid[]) "
                         "AND (status = 'sent' OR sent_at IS NOT NULL)", ([ids["a"], ids["b"]],)).fetchone()[0]
    check(S, "no bot message was sent for the synthetic creators", sent == 0, str(sent))

except Exception as exc:  # noqa: BLE001 - report, then always clean up
    check("suite", "runs to completion", False, f"{type(exc).__name__}: {exc}")
finally:
    for s in sessions:
        s.client.close()
    # Let in-flight worker jobs of the synthetic donations finish before their rows go.
    def _open_jobs():
        with db() as c:
            n = c.execute("SELECT count(*) FROM public.jobs j JOIN public.donations d ON d.id::text = j.dedupe_key "
                          "JOIN public.streamers s ON s.id = d.streamer_id WHERE s.streamer_slug LIKE 'authtest-%%' "
                          "AND j.status IN ('queued', 'running')").fetchone()[0]
        return n == 0
    wait_for(_open_jobs, 60)
    with db() as c:
        c.execute("DELETE FROM public.user_invites WHERE email::text LIKE %s", ("%" + DOMAIN,))
        with fixture_db() as owner:  # review history is append-only for the runtime roles (migration 0021)
            owner.execute("DELETE FROM public.application_events WHERE signup_id IN "
                          "(SELECT id FROM public.user_signups WHERE email LIKE %s)", ("%" + DOMAIN,))
        c.execute("DELETE FROM public.user_signups WHERE email LIKE %s", ("%" + DOMAIN,))
        c.execute("DELETE FROM public.tts_moderation_audit WHERE donation_id IN (SELECT d.id FROM public.donations d "
                  "JOIN public.streamers s ON s.id = d.streamer_id WHERE s.streamer_slug LIKE 'authtest-%%')")
        c.execute("DELETE FROM public.jobs WHERE dedupe_key IN (SELECT d.id::text FROM public.donations d "
                  "JOIN public.streamers s ON s.id = d.streamer_id WHERE s.streamer_slug LIKE 'authtest-%%')")
        c.execute("DELETE FROM public.donations WHERE streamer_id IN "
                  "(SELECT id FROM public.streamers WHERE streamer_slug LIKE 'authtest-%%')")
        c.execute("DELETE FROM public.streamers WHERE streamer_slug LIKE 'authtest-%%'")  # cascades its rows
        c.execute("DELETE FROM public.users WHERE email::text LIKE %s", ("%" + DOMAIN,))  # cascades sessions, tokens
        c.execute("DELETE FROM public.partner_signups WHERE partner_slug LIKE 'authtest-%%'")
        c.execute("DELETE FROM public.partner_api_keys WHERE partner_slug LIKE 'authtest-%%'")
        c.execute("DELETE FROM public.rate_limits WHERE ip_address LIKE 'partner:authtest-%%'")
        c.execute("DELETE FROM public.rate_limits WHERE endpoint LIKE 'auth:%%' OR endpoint LIKE 'admin:%%' "
                  "OR endpoint LIKE 'signup:%%' OR endpoint LIKE 'upload:%%' OR endpoint LIKE 'obs:%%' "
                  "OR endpoint LIKE 'realtime:%%' OR endpoint LIKE 'payment:%%' OR endpoint LIKE 'transcription:%%'")
        left = c.execute("""
            SELECT (SELECT count(*) FROM public.users WHERE email::text LIKE %(d)s),
                   (SELECT count(*) FROM public.sessions s JOIN public.users u ON u.id = s.user_id
                     WHERE u.email::text LIKE %(d)s),
                   (SELECT count(*) FROM public.password_reset_tokens),
                   (SELECT count(*) FROM public.user_invites WHERE email::text LIKE %(d)s),
                   (SELECT count(*) FROM public.user_signups WHERE email LIKE %(d)s),
                   (SELECT count(*) FROM public.streamers WHERE streamer_slug LIKE 'authtest-%%'),
                   (SELECT count(*) FROM public.donations WHERE order_id LIKE 'authtest-%%'),
                   (SELECT count(*) FROM public.streamers_moderators WHERE mod_name = 'Smoke Mod'),
                   (SELECT count(*) FROM public.banned_donors WHERE donor_name LIKE 'Smoke %%'),
                   (SELECT count(*) FROM public.obs_tokens o LEFT JOIN public.streamers s ON s.id = o.streamer_id
                     WHERE s.id IS NULL),
                   (SELECT count(*) FROM public.jobs j LEFT JOIN public.donations d ON d.id::text = j.dedupe_key
                     WHERE j.kind = 'donation_audio' AND d.id IS NULL AND j.created_at > %(t)s),
                   (SELECT count(*) FROM public.soundboard_tnc_acceptances WHERE streamer_slug LIKE 'authtest-%%'),
                   (SELECT count(*) FROM public.partner_api_keys WHERE partner_slug LIKE 'authtest-%%'),
                   (SELECT count(*) FROM public.partner_signups WHERE partner_slug LIKE 'authtest-%%'
                     OR email LIKE %(d)s)
        """, {"d": "%" + DOMAIN, "t": STARTED}).fetchone()
    labels = ("users", "sessions", "reset tokens (all)", "invites", "signups", "creators", "donations", "moderators",
              "bans", "orphan OBS tokens", "jobs", "soundboard terms acceptances",
              "partner keys", "partner signups")
    print("cleanup left: " + ", ".join(f"{k}={v}" for k, v in zip(labels, left)), flush=True)
    check("cleanup", "no synthetic row left", all(v == 0 for v in left), str(dict(zip(labels, left))))

# ── logs: no token, CSRF value, OBS token, reset/invite link or password anywhere ──
since = STARTED.strftime("%Y-%m-%d %H:%M:%S")
journal = subprocess.run(["journalctl", "-u", "hyperchat-api", "-u", "hyperchat-worker", "--since", since,
                          "--utc", "-o", "cat"], capture_output=True, text=True).stdout
nginx = ""
for path in ("/var/log/nginx/access.log", "/var/log/nginx/error.log"):
    try:
        nginx += Path(path).read_text(errors="replace")[-2_000_000:]
    except OSError:
        pass
leaks = [s for s in secrets_seen if s and (s in journal or s in nginx)]
check("logs", "no secret in the API/worker journal or the nginx logs (F1/F1-R)", not leaks, f"{len(leaks)} leaked")

# ── checks this environment cannot run (credentials / approval) ─────────────
for name, why in (
        ("email delivery (reset, invites)", "Resend API key and DEV sender domain not configured (D8)"),
        ("Google OAuth sign-in", "no DEV Google OAuth client (D8)"),
        ("TTS generation (MiniMax / ElevenLabs), !sfx, !testmusic", "provider keys and budget not configured (D8)"),
        ("voice transcription (Sarvam)", "Sarvam key not configured (D8)"),
        ("Razorpay checkout / order creation", "creates Razorpay test-mode orders: needs owner approval"),
        ("payment status poll / confirm with a real test payment (PAY-7)", "owner-run test-mode checkout"),
        ("voice / media / soundboard uploads to R2 (PAY-11 smoke)", "writes R2 objects: needs owner approval"),
        ("bots: Telegram / Discord messages", "bots stay disabled on DEV"),
        ("hosted Supabase rehearsal (D1)", "production-readiness work; separate approval (G1-G6)")):
    blocked("blocked", name, why)

passed = sum(1 for r in results if r[2] == "PASS")
failed = [r for r in results if r[2] == "FAIL"]
blocked_n = sum(1 for r in results if r[2] == "BLOCKED")
print(f"\n{passed} passed, {len(failed)} failed, {blocked_n} blocked (need credentials or approval)")
for section, name, _, detail in failed:
    print(f"  FAILED [{section}] {name}: {detail}")
sys.exit(1 if failed else 0)
