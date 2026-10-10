"""Security audit F5: the public /health hides release, environment and database label from anyone who is not on the
server itself. That rests on the proxy chain, not on the route: Nginx appends the real client address to
X-Forwarded-For ($proxy_add_x_forwarded_for), and Uvicorn, started with --proxy-headers --forwarded-allow-ips
127.0.0.1, trusts the header only from Nginx on 127.0.0.1 and takes the right-most address that is not a trusted
proxy. These tests exercise that boundary at three levels; see docs/backend-security-audit.md (F5) for what each
proves and what remains configuration-audit evidence.

1. The checked-in systemd units and Nginx sites still have exactly the settings the other tests assume.
2. Uvicorn's real ProxyHeadersMiddleware (the class --proxy-headers installs) around the real app: attacker-supplied
   X-Forwarded-For values, through the trusted proxy and from an untrusted peer.
3. A real Nginx (the production site's own forwarding directives) in front of a real Uvicorn process started with the
   systemd unit's proxy flags, requested from a non-loopback address. Needs an nginx binary; skipped otherwise.
"""

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

ROOT = Path(__file__).resolve().parents[2]
UNITS = [ROOT / "deploy/systemd/hyperchat-api.service", ROOT / "deploy/systemd/hyperchat-prod-api.service"]
SITES = [ROOT / "deploy/nginx/api.quicktestspace.site.conf", ROOT / "deploy/nginx/api.hyperchat.space.conf"]
TRUSTED = "127.0.0.1"  # --forwarded-allow-ips in both units (asserted below)
PUBLIC_BODY = {"status": "ok", "database": {"status": "disabled"}}
SECRET_FIELDS = ("release", "environment", "label", "service", "hyperchat-api")


def exec_start(unit: Path) -> str:
    text = unit.read_text(encoding="utf-8").replace("\\\n", " ")
    return re.search(r"^ExecStart=(.+)$", text, re.M).group(1)


def proxy_headers(site: Path) -> list[str]:
    """The proxy_set_header lines of the site's catch-all `location / {` block (the one /health goes through)."""
    block = site.read_text(encoding="utf-8").rsplit("location / {", 1)[1].split("}", 1)[0]
    return [line.strip() for line in block.splitlines() if line.strip().startswith("proxy_set_header")]


# ── 1. configuration the tests below rely on ────────────────────────────────────

@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_uvicorn_trusts_forwarded_headers_only_from_local_nginx(unit):
    cmd = exec_start(unit)
    assert "--host 127.0.0.1" in cmd  # nothing but local processes (Nginx) can connect
    assert "--proxy-headers" in cmd and f"--forwarded-allow-ips {TRUSTED}" in cmd
    assert cmd.count("--forwarded-allow-ips") == 1 and "*" not in cmd.split("--forwarded-allow-ips", 1)[1].split()[0]


@pytest.mark.parametrize("site", SITES, ids=lambda p: p.name)
def test_nginx_appends_the_real_client_address_in_every_proxied_location(site):
    text = site.read_text(encoding="utf-8")
    assert text.count("proxy_pass ") == text.count("proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;") >= 1
    assert "real_ip" not in text and "set_real_ip_from" not in text  # the client address is never rewritten
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in proxy_headers(site)


# ── 2. Uvicorn's real proxy middleware around the real app ──────────────────────

@pytest.fixture
def create_app(monkeypatch):
    monkeypatch.setenv("HYPERCHAT_DB_ENABLED", "false")
    monkeypatch.setenv("HYPERCHAT_APP_ENV", "test")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    yield create_app
    get_settings.cache_clear()


def through_uvicorn(create_app, peer: str) -> TestClient:
    """The app as `uvicorn --proxy-headers --forwarded-allow-ips 127.0.0.1` serves it, reached from `peer`."""
    return TestClient(ProxyHeadersMiddleware(create_app(), trusted_hosts=TRUSTED), client=(peer, 40000))


def assert_public(response):
    assert response.status_code == 200 and response.json() == PUBLIC_BODY
    for field in SECRET_FIELDS:
        assert field not in response.text


@pytest.mark.parametrize("forwarded", [
    "203.0.113.9",                     # a normal public request: Nginx sent the client's address
    "127.0.0.1, 203.0.113.9",          # the attacker sent X-Forwarded-For: 127.0.0.1; Nginx appended the real one
    "::1, 127.0.0.1, 203.0.113.9",     # several forged loopback entries
    "127.0.0.1,203.0.113.9",           # no space after the comma
])
def test_forged_loopback_through_the_trusted_proxy_stays_public(create_app, forwarded):
    with through_uvicorn(create_app, TRUSTED) as nginx_peer:
        assert_public(nginx_peer.get("/health", headers={"X-Forwarded-For": forwarded}))


def test_untrusted_peer_cannot_claim_loopback(create_app):
    """A connection that does not come from the trusted proxy: Uvicorn ignores its X-Forwarded-For entirely."""
    with through_uvicorn(create_app, "198.51.100.7") as outsider:
        assert_public(outsider.get("/health", headers={"X-Forwarded-For": "127.0.0.1"}))
        assert_public(outsider.get("/health"))


def test_genuine_local_health_check_keeps_the_deploy_details(create_app):
    """curl 127.0.0.1:<port>/health on the server: no proxy in between, no X-Forwarded-For."""
    with through_uvicorn(create_app, "127.0.0.1") as local:
        body = local.get("/health").json()
    assert body["service"] == "hyperchat-api" and body["environment"] == "test" and body["release"]
    assert body["database"] == {"label": "DEV", "status": "disabled"}


# ── 3. real Nginx in front of a real Uvicorn process ────────────────────────────

def non_loopback_ipv4() -> str | None:
    """This machine's primary IPv4 address (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("203.0.113.1", 9))
            ip = s.getsockname()[0]
        except OSError:
            return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def free_port(host: str) -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def wait_for(url: str, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=1.0)
            return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise AssertionError(f"{url} did not come up")


@pytest.fixture
def real_chain(tmp_path):
    nginx = shutil.which("nginx") or next((p for p in ("/usr/sbin/nginx", "/usr/local/sbin/nginx")
                                           if os.access(p, os.X_OK)), None)
    public_ip = non_loopback_ipv4()
    if nginx is None or public_ip is None or sys.platform == "win32":
        pytest.skip("needs an nginx binary and a non-loopback IPv4 address (runs in CI)")
    upstream, front = free_port("127.0.0.1"), free_port(public_ip)
    unit_flags = exec_start(UNITS[1]).split("app.main:app", 1)[1].split()
    flags = []  # the production unit's flags, with this test's port and a single worker
    skip = {"--port": 2, "--workers": 2}
    i = 0
    while i < len(unit_flags):
        if unit_flags[i] in skip:
            i += skip[unit_flags[i]]
            continue
        flags.append(unit_flags[i])
        i += 1
    env = {**os.environ, "HYPERCHAT_DB_ENABLED": "false", "HYPERCHAT_APP_ENV": "test"}
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", *flags, "--port", str(upstream)],
                           cwd=ROOT / "backend", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for d in ("logs", "cb", "px", "fc", "uw", "sc"):
        (tmp_path / d).mkdir()
    headers = "\n            ".join(proxy_headers(SITES[1]))  # the production site's own forwarding directives
    (tmp_path / "nginx.conf").write_text(f"""
pid {tmp_path}/nginx.pid;
error_log {tmp_path}/logs/error.log warn;
events {{}}
http {{
    access_log off;
    client_body_temp_path {tmp_path}/cb; proxy_temp_path {tmp_path}/px; fastcgi_temp_path {tmp_path}/fc;
    uwsgi_temp_path {tmp_path}/uw; scgi_temp_path {tmp_path}/sc;
    server {{
        listen {public_ip}:{front};
        location / {{
            proxy_pass http://127.0.0.1:{upstream};
            proxy_http_version 1.1;
            {headers}
        }}
    }}
}}
""", encoding="utf-8")
    proxy = None
    try:
        wait_for(f"http://127.0.0.1:{upstream}/health")
        proxy = subprocess.Popen([nginx, "-e", str(tmp_path / "logs/startup.log"), "-p", str(tmp_path),
                                  "-c", str(tmp_path / "nginx.conf"), "-g", "daemon off;"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for(f"http://{public_ip}:{front}/health")
        yield {"front": f"http://{public_ip}:{front}", "local": f"http://127.0.0.1:{upstream}", "client": public_ip}
    finally:
        for process in (proxy, api):
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()


def test_real_nginx_and_uvicorn_never_treat_a_forged_header_as_local(real_chain):
    """Real Nginx (production forwarding directives) -> real Uvicorn (production proxy flags) -> the app, requested
    from a non-loopback address of this machine, with and without an attacker-supplied X-Forwarded-For."""
    transport = httpx.HTTPTransport(local_address=real_chain["client"])  # the request comes from a non-loopback address
    with httpx.Client(transport=transport, timeout=5.0) as client:
        for headers in ({}, {"X-Forwarded-For": "127.0.0.1"}, {"X-Forwarded-For": "::1, 127.0.0.1"},
                        {"X-Real-IP": "127.0.0.1", "X-Forwarded-For": "127.0.0.1"}):
            assert_public(client.get(f"{real_chain['front']}/health", headers=headers))
    local = httpx.get(f"{real_chain['local']}/health", timeout=5.0).json()  # the deploy check on the server
    assert local["environment"] == "test" and local["release"] and local["database"]["label"] == "DEV"
