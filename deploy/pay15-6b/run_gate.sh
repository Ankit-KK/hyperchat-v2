# The ONLY entry point on DEV:  bash /root/deploy-dev-6b-<NEW_ID>/pkg/run_gate.sh <G0|B|R|W|S|X>
# It is started from the owner's machine by a reviewed one-line wrapper FILE, never an inline command (incident
# 2026-10-28):  cmd /c "ssh root@<DEV host> bash -s < remote\gate_G0.sh"   (README §2; one wrapper per gate).
# FIRST, before anything is read or written, the staged package must match its manifest exactly; then the gate's own
# preflight runs, and any refusal stops before a change. Output is kept in $ST/gate-<X>-<UTC>.log (root 0600). (C1)
set -u
umask 077
[ "$(id -u)" = 0 ] || { echo "STOP: run as root"; exit 1; }
G="${1:-}"
case "$G" in G0) F=g0_precheck.sh ;; B) F=b_build.sh ;; R) F=r_rehearsal.sh ;; W) F=w_window.sh ;;
             S) F=s_post.sh ;; X) F=x_rollback.sh ;;
             *) echo "STOP: usage: run_gate.sh G0|B|R|W|S|X"; exit 2 ;; esac
P="$(dirname "$(readlink -f "$0")")"
( cd "$P" && sha256sum -c --quiet MANIFEST.sha256 ) >/dev/null 2>&1 \
  && [ "$(cd "$P" && find . -type f ! -name MANIFEST.sha256 | sed 's#^\./##' | LC_ALL=C sort)" = "$(sed 's/^[0-9a-f]* \*//' "$P/MANIFEST.sha256" | LC_ALL=C sort)" ] \
  || { echo "STOP: the staged package does not match its manifest exactly (modified, missing or extra files)"; exit 1; }
[ "${B6_TEST:-}" != 1 ] || [ ! -e /opt/hyperchat ] || { echo "STOP: test mode is refused on a server"; exit 1; }
echo "gate $G: staged package matches its manifest exactly ($(sha256sum "$P/MANIFEST.sha256" | cut -c1-16))"
. "$P/common.sh"
if [ "${B6_TEST:-}" != 1 ]; then
  [ "$P" = "/root/deploy-dev-6b-$NEW_ID/pkg" ] || { echo "STOP: the package must be staged at /root/deploy-dev-6b-$NEW_ID/pkg"; exit 1; }
  [ -d "$ST" ] && [ "$(stat -c '%U %a' "$ST")" = "root 700" ] || { echo "STOP: $ST is not a root 0700 directory"; exit 1; }
else
  [ -d "$ST" ] || { echo "STOP: $ST is missing"; exit 1; }
  [ -z "${B6_STUBS:-}" ] || . "$B6_STUBS"   # LOCAL / CI mock tests only (fake system state)
fi
LOG="$ST/gate-$G-$(date -u +%Y%m%dT%H%M%SZ).log"
set -o pipefail
( . "$P/$F" ) 2>&1 | tee "$LOG"
rc=$?
echo "gate $G exit $rc; log $LOG"
exit $rc
