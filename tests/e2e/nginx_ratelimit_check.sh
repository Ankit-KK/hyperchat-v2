#!/usr/bin/env bash
# Exercise the public-API rate limit with the REAL snippets (deploy/nginx/snippets/) in a throwaway nginx
# bound to 127.0.0.1 (no TLS, no proxying, nothing installed). Also syntax-checks the dev site file with
# its includes pointed at the repository snippets. Run on a host with nginx, e.g. the VPS:
#     bash tests/e2e/nginx_ratelimit_check.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SNIP="$ROOT/deploy/nginx/snippets"
T="$(mktemp -d)"; PORT="${HYPERCHAT_NGINX_TEST_PORT:-18089}"
cleanup() { nginx -p "$T" -c "$T/nginx.conf" -s quit 2>/dev/null || true; sleep 0.3; rm -rf "$T"; }
trap cleanup EXIT
mkdir -p "$T/logs" "$T/www" && echo ok > "$T/www/ok.txt"
chmod 755 "$T" "$T/www" && chmod 644 "$T/www/ok.txt"  # nginx workers (www-data) must read the file

cat > "$T/nginx.conf" <<EOF
pid $T/nginx.pid;
error_log $T/logs/error.log warn;
events {}
http {
    access_log off;
    client_body_temp_path $T/cb; proxy_temp_path $T/px; fastcgi_temp_path $T/fc; uwsgi_temp_path $T/uw; scgi_temp_path $T/sc;
    include $SNIP/hyperchat-public-limit-zone.conf;
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
}
EOF
nginx -t -q -p "$T" -c "$T/nginx.conf"
nginx -p "$T" -c "$T/nginx.conf"

burst() {  # $1 = path, $2 = number of back-to-back requests -> "<200 count> <429 count>"
    local ok=0 limited=0 code
    for _ in $(seq 1 "$2"); do
        code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT$1")
        if [ "$code" = 200 ]; then ok=$((ok + 1)); elif [ "$code" = 429 ]; then limited=$((limited + 1)); fi
    done
    echo "$ok $limited"
}

fail=0
read -r ok limited < <(burst /api/v1/public/streamers/x 40)
echo "public: $ok x 200, $limited x 429 (40 back-to-back)"
{ [ "$ok" -ge 21 ] && [ "$ok" -le 25 ] && [ "$limited" -ge 15 ]; } || { echo "FAIL public burst"; fail=1; }
body=$(curl -s "http://127.0.0.1:$PORT/api/v1/metrics/platform")
echo "429 body: $body"
[ "$body" = '{"error":"Too many requests"}' ] || { echo "FAIL 429 body"; fail=1; }
read -r ok limited < <(burst /api/v1/auth/session 40)
echo "non-public: $ok x 200, $limited x 429"
[ "$limited" -eq 0 ] || { echo "FAIL non-public path limited"; fail=1; }
sleep 3
read -r ok limited < <(burst /api/v1/public/streamers/x 5)
echo "after 3s: $ok x 200, $limited x 429"
[ "$ok" -eq 5 ] || { echo "FAIL recovery"; fail=1; }

# Syntax-check the real dev site file with includes redirected to the repository snippets.
sed "s#/etc/nginx/snippets/#$SNIP/#g" "$ROOT/deploy/nginx/api.quicktestspace.site.conf" > "$T/site.conf"
cat > "$T/site-test.conf" <<EOF
pid $T/site.pid;
error_log $T/logs/site-error.log warn;
events {}
http { include $T/site.conf; }
EOF
if nginx -t -q -p "$T" -c "$T/site-test.conf"; then echo "site config syntax: ok"; else echo "FAIL site config"; fail=1; fi

[ "$fail" -eq 0 ] && echo "nginx rate-limit check: PASS" || { echo "nginx rate-limit check: FAIL"; exit 1; }
