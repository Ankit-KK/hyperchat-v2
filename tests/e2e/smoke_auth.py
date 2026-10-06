"""End-to-end smoke check of a DEPLOYED API's authentication over public HTTPS.

Runs on the VPS from a checkout of this repository (it needs peer access to the API's database
to create one synthetic account):
    /opt/hyperchat/current/venv/bin/python tests/e2e/smoke_auth.py
Environment (defaults = v2 development):
    HYPERCHAT_SMOKE_API   https://api.quicktestspace.site
    HYPERCHAT_SMOKE_DB    hyperchat_dev:5433   (never hyperchat or hyperchat_restore_test)
    HYPERCHAT_SMOKE_SAMESITE  lax              expected SameSite of the session cookie: the secure
                                               default; the v2 dev API uses "none" (cross-site dev)
    HYPERCHAT_SMOKE_ORIGIN    https://app.quicktestspace.site   the frontend origin the API trusts
                                               (its HYPERCHAT_FRONTEND_URL); production only at cutover
The synthetic account (@authtest.invalid) and its sessions are deleted afterwards; the password is
random per run and nothing secret is printed. Exit code 0 = every check passed.
"""

import os
import secrets
import subprocess
import sys
from pathlib import Path

import httpx
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.auth.passwords import hash_password  # noqa: E402
from app.auth.tokens import sha256_hex  # noqa: E402

API = os.environ.get("HYPERCHAT_SMOKE_API", "https://api.quicktestspace.site").rstrip("/")
DB_NAME, DB_PORT = os.environ.get("HYPERCHAT_SMOKE_DB", "hyperchat_dev:5433").split(":")
if DB_NAME in ("hyperchat", "hyperchat_restore_test"):
    sys.exit("refusing to create test accounts in production or the restore source")
SAMESITE = os.environ.get("HYPERCHAT_SMOKE_SAMESITE", "lax").strip().lower()
if SAMESITE not in ("lax", "strict", "none"):
    sys.exit("HYPERCHAT_SMOKE_SAMESITE must be lax, strict or none")
FRONT = os.environ.get("HYPERCHAT_SMOKE_ORIGIN", "https://app.quicktestspace.site").strip().rstrip("/")
if not FRONT.startswith(("https://", "http://localhost")) or FRONT.count("/") != 2:
    sys.exit("HYPERCHAT_SMOKE_ORIGIN must be an origin such as https://app.quicktestspace.site")
ORIGIN = {"origin": FRONT}
EMAIL = "devsmoke@authtest.invalid"
PASSWORD = secrets.token_urlsafe(18)  # never printed
DSN = f"host=/var/run/postgresql port={DB_PORT} dbname={DB_NAME} user=hyperchat_app"
results = []


def check(name, ok):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name)


with psycopg.connect(DSN, autocommit=True) as db:
    assert db.execute("select current_database()").fetchone()[0] == DB_NAME
    db.execute("INSERT INTO public.users (email, password_hash, role) VALUES (%s, %s, 'streamer')",
               (EMAIL, hash_password(PASSWORD)))

seen_tokens = []
try:
    with httpx.Client(base_url=API, timeout=15) as c:
        r = c.get("/api/v1/auth/session")
        check("GET /session without cookie -> 401", r.status_code == 401)
        r = c.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD},
                   headers={"origin": "https://evil.invalid"})
        check("login from foreign Origin -> 403", r.status_code == 403)
        a = c.post("/api/v1/auth/login", json={"email": EMAIL, "password": "wrong-" + PASSWORD}, headers=ORIGIN)
        b = c.post("/api/v1/auth/login", json={"email": "nobody@authtest.invalid", "password": "x"}, headers=ORIGIN)
        check("wrong password / unknown email -> identical generic 401",
              a.status_code == b.status_code == 401 and a.json() == b.json() == {"error": "Invalid email or password"})

        r = c.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD},
                   headers={**ORIGIN, "x-forwarded-for": "203.0.113.9"})
        check("login -> 200", r.status_code == 200)
        ck = r.headers.get("set-cookie", "")
        low = ck.lower()
        check(f"cookie __Host-hc_session; Secure; HttpOnly; SameSite={SAMESITE}; Path=/; no Domain",
              ck.startswith("__Host-hc_session=") and "secure" in low and "httponly" in low
              and f"samesite={SAMESITE}" in low and "path=/" in low and "domain=" not in low)
        check("CORS echoes allowed origin with credentials",
              r.headers.get("access-control-allow-origin") == FRONT
              and r.headers.get("access-control-allow-credentials") == "true")
        csrf = r.json()["csrf_token"]
        token = c.cookies.get("__Host-hc_session")
        seen_tokens += [token, csrf]

        with psycopg.connect(DSN) as db:
            row = db.execute("SELECT s.token_hash, host(s.ip_address) FROM public.sessions s JOIN public.users u "
                             "ON u.id = s.user_id WHERE u.email = %s", (EMAIL,)).fetchone()
        check("DB stores only SHA-256 of the session token", row[0] == sha256_hex(token) and row[0] != token)
        check("spoofed X-Forwarded-For ignored (recorded IP is the real client)", row[1] not in (None, "203.0.113.9"))

        r = c.get("/api/v1/auth/session")
        check("GET /session with cookie -> 200 + same CSRF token", r.status_code == 200 and r.json()["csrf_token"] == csrf)
        check("POST /logout without CSRF token -> 403", c.post("/api/v1/auth/logout", headers=ORIGIN).status_code == 403)
        check("POST /logout with wrong CSRF token -> 403",
              c.post("/api/v1/auth/logout", headers={**ORIGIN, "x-csrf-token": "0" * 64}).status_code == 403)
        check("POST /logout with CSRF token but foreign Origin -> 403",
              c.post("/api/v1/auth/logout", headers={"origin": "https://evil.invalid", "x-csrf-token": csrf}).status_code == 403)
        check("POST /invites as non-admin -> 403",
              c.post("/api/v1/auth/invites", json={"email": "x@authtest.invalid"},
                     headers={**ORIGIN, "x-csrf-token": csrf}).status_code == 403)
        r = c.post("/api/v1/auth/password/forgot", json={"email": EMAIL})
        r2 = c.post("/api/v1/auth/password/forgot", json={"email": "nobody@authtest.invalid"})
        check("forgot password -> 202, identical for unknown email",
              r.status_code == r2.status_code == 202 and r.json() == r2.json())
        check("Google sign-in disabled -> 404",
              c.get("/api/v1/auth/google/start", follow_redirects=False).status_code == 404)
        r = c.post("/api/v1/auth/logout", headers={**ORIGIN, "x-csrf-token": csrf})
        check("POST /logout with Origin + CSRF -> 200", r.status_code == 200)
        check("session revoked after logout -> 401", c.get("/api/v1/auth/session").status_code == 401)
        replay = httpx.get(API + "/api/v1/auth/session", headers={"cookie": f"__Host-hc_session={token}"}, timeout=15)
        check("replayed revoked cookie -> 401", replay.status_code == 401)
        pre = httpx.options(API + "/api/v1/auth/login", headers={**ORIGIN, "access-control-request-method": "POST"},
                            timeout=15)
        check("CORS preflight from frontend origin -> 200", pre.status_code == 200
              and pre.headers.get("access-control-allow-origin") == FRONT)
finally:
    with psycopg.connect(DSN, autocommit=True) as db:
        db.execute("DELETE FROM public.users WHERE email::text LIKE '%%@authtest.invalid'")
        db.execute("DELETE FROM public.rate_limits WHERE endpoint LIKE 'auth:%%'")
        left = db.execute("SELECT (SELECT count(*) FROM public.users WHERE email::text LIKE '%%@authtest.invalid'),"
                          " (SELECT count(*) FROM public.sessions), (SELECT count(*) FROM public.password_reset_tokens)"
                          ).fetchone()
    print("cleanup: synthetic users / sessions / reset tokens left =", left)

journal = subprocess.run(["journalctl", "-u", "hyperchat-api", "--since", "-15min", "-o", "cat"],
                         capture_output=True, text=True).stdout
check("no session token, CSRF token or password in the service journal",
      all(t and t not in journal for t in seen_tokens) and PASSWORD not in journal)
failed = [n for n, ok in results if not ok]
print(f"{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
