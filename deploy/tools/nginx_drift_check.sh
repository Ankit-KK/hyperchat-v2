#!/usr/bin/env bash
# nginx drift check (OPS-2 safeguard S4; incident 2026-10-28). Read-only: writes nothing.
#
#   bash deploy/tools/nginx_drift_check.sh                 local mode (default): exit 0 always
#   bash deploy/tools/nginx_drift_check.sh --nginx-test    HOST ONLY, needs separate approval: also runs `nginx -t`
#
# For every file under deploy/nginx/ it prints one line comparing the repository file's sha256 with the recorded
# DEPLOYED fingerprint in nginx_fingerprints.txt:
#   match       the repository file is the recorded deployed version
#   drift       repo differs from recorded deployed (normal when the repo moved past what is installed)
#   unrecorded  no deployed fingerprint is recorded for this file
# plus `missing` for a recorded row whose file is no longer under deploy/nginx/. Exit 2 without the fingerprints file.
# It names differences; it never asserts that the repository equals the server.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
NGINX="$ROOT/deploy/nginx"
FP="$ROOT/deploy/tools/nginx_fingerprints.txt"
case "${1:-}" in
  "") NGINX_TEST=0 ;;
  --nginx-test) NGINX_TEST=1 ;;
  *) echo "usage: $0 [--nginx-test]" >&2; exit 2 ;;
esac
[ -r "$FP" ] || { echo "missing or unreadable $FP" >&2; exit 2; }

while IFS= read -r f; do
  rel="${f#"$NGINX"/}"
  hash="$(sha256sum < "$f" | cut -c1-64)"
  recorded="$(awk -v p="$rel" '!/^#/ && $2 == p { print $1; exit }' "$FP")"
  if [[ ! "$recorded" =~ ^[0-9a-f]{8,64}$ ]]; then
    echo "unrecorded  $rel  (repo ${hash:0:16})"
  elif [[ "$hash" == "$recorded"* ]]; then
    echo "match       $rel  (deployed $recorded)"
  else
    echo "drift       $rel  (repo differs from recorded deployed: repo ${hash:0:16}, deployed $recorded)"
  fi
done < <(find "$NGINX" -type f | LC_ALL=C sort)
# A recorded row whose file is gone from the repository is named too (a rename or a removal to review).
while read -r _ rel _; do
  [ -f "$NGINX/$rel" ] || echo "missing     $rel  (in nginx_fingerprints.txt, not under deploy/nginx/)"
done < <(grep -v '^#' "$FP" | grep -v '^[[:space:]]*$')

if [ "$NGINX_TEST" -eq 1 ]; then
  # Host only (root on the server), and only with separate approval: checks the INSTALLED configuration.
  nginx -t
  exit $?
fi
exit 0
