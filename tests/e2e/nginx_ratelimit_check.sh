#!/usr/bin/env bash
# Exercise the public-API rate limit with the REAL snippets (deploy/nginx/snippets/) in a throwaway nginx
# bound to 127.0.0.1 (no TLS, no proxying, nothing installed): the DEV zone and the separate PRODUCTION
# zone. Also syntax-checks the DEV site, the PRODUCTION site (bootstrap and full phase) and the catch-all
# loaded TOGETHER, with includes pointed at the repository snippets and a throwaway self-signed
# certificate standing in for Let's Encrypt. Run on a host with nginx and openssl, e.g. the VPS or CI:
#     bash tests/e2e/nginx_ratelimit_check.sh
#
# Isolation: every nginx started or syntax-checked here uses ONLY paths under a fresh mktemp directory
# (pid, startup and runtime error logs, access log, all five temp directories). nginx run as root takes
# ownership of whatever temp directories its configuration names, so a config without them would fall back
# to the compiled-in system ones (/var/lib/nginx/*) and hand them to its default user, breaking the host's
# nginx (request bodies > 16 KB). A guard compares owner/mode of the host's nginx state directories
# (distro paths + this build's compiled-in temp/log paths) before and after the run and fails on any change.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SNIP="$ROOT/deploy/nginx/snippets"
T="$(mktemp -d)"; PORT="${HYPERCHAT_NGINX_TEST_PORT:-18089}"; PORT_PROD=$((PORT + 1))
ngx() { nginx -e "$T/logs/startup-error.log" -p "$T" "$@"; }  # -e: even the pre-config error log stays in $T
cleanup() { ngx -c "$T/nginx.conf" -s quit 2>/dev/null || true; sleep 0.3; rm -rf "$T"; }
trap cleanup EXIT
mkdir -p "$T/logs" "$T/www" && echo ok > "$T/www/ok.txt"
# The host's nginx state directories: the distro locations plus this build's compiled-in temp/log paths
# (all five temp paths incl. http-client-body-temp-path; non-absolute values such as "stderr" are skipped).
host_dirs() {
    { printf '%s\n' /var/lib/nginx /var/log/nginx /var/cache/nginx
      nginx -V 2>&1 | grep -oE -- '--(http-[a-z-]+-temp-path|http-log-path|error-log-path)=[^ ]+' | cut -d= -f2 \
          | grep '^/' | xargs -r -n1 dirname
    } | sort -u
}
# Owner/group/mode of those directories and their direct entries (not contents or times: live traffic changes those).
host_state() { for d in $(host_dirs); do [ -e "$d" ] && find "$d" -maxdepth 1 -exec stat -c '%n %U:%G %a' {} +; done 2>/dev/null | sort; }
HOST_BEFORE="$(host_state)"
# Paths that would otherwise default to the compiled-in system locations.
ISOLATED="client_body_temp_path $T/cb; proxy_temp_path $T/px; fastcgi_temp_path $T/fc; uwsgi_temp_path $T/uw; scgi_temp_path $T/sc; access_log $T/logs/access.log;"
chmod 755 "$T" "$T/www" && chmod 644 "$T/www/ok.txt"  # nginx workers (www-data) must read the file

cat > "$T/nginx.conf" <<EOF
pid $T/nginx.pid;
error_log $T/logs/error.log warn;
events {}
http {
    $ISOLATED
    include $SNIP/hyperchat-public-limit-zone.conf;
    include $SNIP/hyperchat-prod-public-limit-zone.conf;
    server {
        listen 127.0.0.1:$PORT;
        # Serve a file (content phase, like proxy_pass in production): a "return" would run in the
        # rewrite phase, before limit_req (preaccess) ever sees the request.
        root $T/www;
        location ~ ^/api/v1/(public|metrics)/ {
            include $SNIP/hyperchat-public-limit.conf;
            try_files /ok.txt =404;
        }
        location @hyperchat_rate_limited {
            default_type application/json;
            return 429 '{"error":"Too many requests"}';
        }
        location / { try_files /ok.txt =404; }
    }
    server {  # production zone, same behaviour, own counters
        listen 127.0.0.1:$PORT_PROD;
        root $T/www;
        location ~ ^/api/v1/(public|metrics)/ {
            include $SNIP/hyperchat-prod-public-limit.conf;
            try_files /ok.txt =404;
        }
        location @hyperchat_rate_limited {
            default_type application/json;
            return 429 '{"error":"Too many requests"}';
        }
    }
}
EOF
ngx -t -q -c "$T/nginx.conf"
ngx -c "$T/nginx.conf"

burst() {  # $1 = path, $2 = number of back-to-back requests, $3 = port -> "<200 count> <429 count>"
    local ok=0 limited=0 code
    for _ in $(seq 1 "$2"); do
        code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${3:-$PORT}$1")
        if [ "$code" = 200 ]; then ok=$((ok + 1)); elif [ "$code" = 429 ]; then limited=$((limited + 1)); fi
    done
    echo "$ok $limited"
}

limited_body() {  # $1 = port -> the body of the first 429 among up to 10 quick requests ("none" if no 429)
    # Sampling until a 429 avoids a timing race: on a slow runner the bucket can refill one token between
    # the burst and a single follow-up request, which is then (correctly) allowed with 200.
    local out code
    for _ in $(seq 1 10); do
        out=$(curl -s -w '\n%{http_code}' "http://127.0.0.1:$1/api/v1/metrics/platform")
        code=${out##*$'\n'}
        if [ "$code" = 429 ]; then echo "${out%$'\n'*}"; return; fi
    done
    echo none
}

fail=0
read -r ok limited < <(burst /api/v1/public/streamers/x 40)
echo "public: $ok x 200, $limited x 429 (40 back-to-back)"
{ [ "$ok" -ge 21 ] && [ "$ok" -le 25 ] && [ "$limited" -ge 15 ]; } || { echo "FAIL public burst"; fail=1; }
body=$(limited_body "$PORT")
echo "429 body: $body"
[ "$body" = '{"error":"Too many requests"}' ] || { echo "FAIL 429 body"; fail=1; }
read -r ok limited < <(burst /api/v1/auth/session 40)
echo "non-public: $ok x 200, $limited x 429"
[ "$limited" -eq 0 ] || { echo "FAIL non-public path limited"; fail=1; }
sleep 3
read -r ok limited < <(burst /api/v1/public/streamers/x 5)
echo "after 3s: $ok x 200, $limited x 429"
[ "$ok" -eq 5 ] || { echo "FAIL recovery"; fail=1; }
read -r ok limited < <(burst /api/v1/public/streamers/x 40 "$PORT_PROD")
echo "production zone: $ok x 200, $limited x 429 (40 back-to-back)"
{ [ "$ok" -ge 21 ] && [ "$ok" -le 25 ] && [ "$limited" -ge 15 ]; } || { echo "FAIL production burst"; fail=1; }
body=$(limited_body "$PORT_PROD")
[ "$body" = '{"error":"Too many requests"}' ] || { echo "FAIL production 429 body"; fail=1; }

# Syntax-check the real site files, all loaded together as on the server: includes redirected to the
# repository snippets, Let's Encrypt paths to a throwaway self-signed certificate.
mkdir -p "$T/certs"
openssl req -x509 -nodes -newkey rsa:2048 -days 1 -subj "/CN=nginx-check.invalid" \
    -keyout "$T/certs/privkey.pem" -out "$T/certs/fullchain.pem" >/dev/null 2>&1
site() { sed -e "s#/etc/nginx/snippets/#$SNIP/#g" -e "s#/etc/letsencrypt/live/[^/]*/#$T/certs/#g" "$ROOT/deploy/nginx/$1"; }
syntax() {  # $1 = label, $2... = site files loaded together
    local label="$1"; shift; : > "$T/sites.conf"
    for f in "$@"; do site "$f" >> "$T/sites.conf"; done
    cat > "$T/site-test.conf" <<EOF
pid $T/site.pid;
error_log $T/logs/site-error.log warn;
events {}
http { $ISOLATED include $T/sites.conf; }
EOF
    if ngx -t -q -c "$T/site-test.conf"; then echo "syntax $label: ok"; else echo "FAIL syntax $label"; fail=1; fi
}
syntax "dev site" api.quicktestspace.site.conf
syntax "catch-all + dev + production bootstrap (port 80)" 00-catchall.conf api.quicktestspace.site.conf api.hyperchat.space.acme.conf
syntax "catch-all + dev + production (80 + 443)" 00-catchall.conf api.quicktestspace.site.conf api.hyperchat.space.conf

# Access logs (security audit F1): run the REAL DEV and production site files, with their listen ports, log file,
# ACME root and upstream pointed into this sandbox (the upstream is a stub that answers 200), send requests that
# carry a secret marker in the query string and in the Referer, and require that the marker never reaches the
# log. Production must drop every query string and referrer; DEV drops them under /api/v1/obs/ and
# /api/v1/realtime/. A control proves the log would show a query string if one were written.
P80=$((PORT + 2)); P443=$((PORT + 3)); STUB=$((PORT + 4))
SECRET="obs_logcanary$$$RANDOM"; CONTROL="visiblecontrol$$$RANDOM"
logsite() { site "$1" | sed -e "s#listen 80;#listen 127.0.0.1:$P80;#" -e "s#listen 443 ssl;#listen 127.0.0.1:$P443 ssl;#" \
    -e "s#/var/log/nginx/access.log#$T/logs/site-access.log#g" -e "s#root /var/www/acme;#root $T/www;#" \
    -e "s#proxy_pass http://127.0.0.1:800[01];#proxy_pass http://127.0.0.1:$STUB;#"; }
{ logsite api.quicktestspace.site.conf; logsite api.hyperchat.space.conf; } > "$T/logtest-sites.conf"
cat > "$T/logtest.conf" <<EOF
pid $T/logtest.pid;
error_log $T/logs/logtest-error.log warn;
events {}
http {
    $ISOLATED
    server { listen 127.0.0.1:$STUB; access_log off; return 200 "stub\n"; }
    include $T/logtest-sites.conf;
}
EOF
ngx -t -q -c "$T/logtest.conf" && ngx -c "$T/logtest.conf"
tls() {  # $1 = host, $2 = path and query
    curl -sk -o /dev/null -w '%{http_code}' -H "Referer: https://evil.invalid/?token=$SECRET" \
        --resolve "$1:$P443:127.0.0.1" "https://$1:$P443$2"
}
codes=""
for path in "/api/v1/obs/audio/next?token=$SECRET" "/api/v1/realtime/pusher/auth?token=$SECRET" \
            "/api/v1/public/streamers/x?token=$SECRET" "/api/v1/auth/session?reset=$SECRET"; do
    codes="$codes $(tls api.hyperchat.space "$path")"
done
codes="$codes $(curl -s -o /dev/null -w '%{http_code}' -H 'Host: api.hyperchat.space' \
    "http://127.0.0.1:$P80/api/v1/obs/audio/next?token=$SECRET")"  # port 80: the redirect to HTTPS
for path in "/api/v1/obs/audio/next?token=$SECRET" "/api/v1/realtime/pusher/auth?token=$SECRET"; do
    codes="$codes $(tls api.quicktestspace.site "$path")"
done
codes="$codes $(curl -sk -o /dev/null -w '%{http_code}' --resolve "api.quicktestspace.site:$P443:127.0.0.1" \
    "https://api.quicktestspace.site:$P443/api/v1/auth/session?control=$CONTROL")"  # DEV logs other queries in full
ngx -c "$T/logtest.conf" -s quit 2>/dev/null || true; sleep 0.3
echo "access-log requests:$codes"
[ "$codes" = " 200 200 200 200 301 200 200 200" ] || { echo "FAIL access-log requests did not reach the sites"; fail=1; }
lines=$(grep -c . "$T/logs/site-access.log" || true)
echo "access-log lines written: $lines"
[ "$lines" -eq 8 ] || { echo "FAIL access log should have 8 lines"; fail=1; }
if grep -q "$SECRET" "$T/logs/site-access.log"; then
    echo "FAIL a query string or referrer secret reached the access log:"; grep -n "$SECRET" "$T/logs/site-access.log" | sed "s/$SECRET/<secret>/g"; fail=1
else echo "access log: no query-string or referrer secret (production: every request; DEV: /obs/, /realtime/)"; fi
grep -q "$CONTROL" "$T/logs/site-access.log" || { echo "FAIL control: the DEV query string was not logged, so this check proves nothing"; fail=1; }
[ "$(grep -c 'GET /api/v1/obs/audio/next HTTP' "$T/logs/site-access.log" || true)" -eq 3 ] \
    || { echo "FAIL the redacted lines should still show the path"; fail=1; }

ngx -c "$T/nginx.conf" -s quit 2>/dev/null || true; sleep 0.3
if [ "$(host_state)" = "$HOST_BEFORE" ]; then echo "host nginx state ($(host_dirs | tr '\n' ' ')): unchanged"
else echo "FAIL host nginx state changed:"; diff <(echo "$HOST_BEFORE") <(host_state) || true; fail=1; fi

[ "$fail" -eq 0 ] && echo "nginx rate-limit check: PASS" || { echo "nginx rate-limit check: FAIL"; exit 1; }
