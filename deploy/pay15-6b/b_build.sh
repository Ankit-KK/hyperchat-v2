# B: build and verify the release directory $REL_NEW. NOTHING is switched, migrated, stopped or restarted; api.env
# untouched. The only change is the new, inert release directory (C1 gate B, with the r2 lock fix: hash-pinned lock,
# no resolution, then the release's own pins with no index, then the exact freeze guard against the running release).
b_build() { local old rel
  [ -f "$ST/G0-passed" ] && [ ! -e "$ST/B-passed" ] || { echo "STOP: G0 has not passed, or B already passed"; return 1; }
  old="$(facts OLD_ID)"; rel="$(facts OLD_REL)"
  echo "== B0 preflight (read-only): nothing changed since G0"
  host_ok && target_ok && head_is "$PRE_HEAD" && [ "$(readlink -f "$APP_CURRENT")" = "$rel" ] && api_healthy "$old" >/dev/null \
    && [ "$(env_sha)" = "$(facts ENV_SHA)" ] && [ ! -e "$REL_NEW" ] || { echo "STOP: DEV is not in the G0 state"; return 1; }
  echo "== B1 build"
  [ "$(sha256sum "$TAR" | cut -c1-64)" = "$TAR_SHA" ] || { echo "STOP: the release archive is not the pinned one"; return 1; }
  mkdir "$REL_NEW" && tar -x -C "$REL_NEW" -f "$TAR" || { echo "STOP: extract"; return 1; }
  [ "$(fp "$REL_NEW")" = "$NEW_FP" ] && [ "$(find "$REL_NEW" -type f | wc -l)" = "$NEW_FILES" ] || { echo "STOP: $REL_NEW is not the $COMMIT tree"; return 1; }
  pins_ok "$REL_NEW" || return 1
  [ "$(ls "$REL_NEW/database/migrations/versions" | grep -E '^[0-9]{4}_' | sort | tail -1)" = 0035_payment_accounting_hardening.py ] \
    || { echo "STOP: the release's newest migration is not 0035"; return 1; }
  [ "$(sha256sum "$LOCK" | cut -c1-64)" = "$LOCK_SHA" ] || { echo "STOP: the dependency lock is not the pinned one"; return 1; }
  python3.14 -m venv "$REL_NEW/venv" && "$REL_NEW/venv/bin/pip" install -q --require-hashes --no-deps -r "$LOCK" \
    && "$REL_NEW/venv/bin/pip" install -q --no-index --no-deps -r "$REL_NEW/backend/requirements.txt" || { echo "STOP: venv"; return 1; }
  echo "$NEW_ID" > "$REL_NEW/backend/RELEASE" && chown -R root:hyperchat "$REL_NEW" && chmod -R g+rX,g-w,o-rwx "$REL_NEW" \
    || { echo "STOP: RELEASE / ownership"; return 1; }
  ( cd "$REL_NEW/backend" && sudo -u hyperchat env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 ../venv/bin/python -B -c "import app.main" ) \
    || { echo "STOP: import app.main"; return 1; }
  "$REL_NEW/venv/bin/pip" check >/dev/null || { echo "STOP: pip check"; return 1; }
  "$REL_NEW/venv/bin/pip" freeze | LC_ALL=C sort > "$ST/pip-new.txt"
  diff "$ST/pip-old.txt" "$ST/pip-new.txt" > "$ST/pip-freeze.diff" \
    || { echo "STOP: dependency versions differ from $old (requirements.txt is unchanged since 0963113):"; cat "$ST/pip-freeze.diff"; return 1; }
  echo "dependencies identical to $old ($(wc -l < "$ST/pip-new.txt") packages)"
  echo "== B2 the built bytes refuse a true accounting setting without 0035 (no database, no network, api.env never read)"
  ( cd "$REL_NEW/backend" && sudo -u hyperchat env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 ../venv/bin/python -B -c '
import asyncio
from app.payments import switch
async def boom():
    raise OSError("no database in the probe")
class C:
    async def __aenter__(self): return await boom()
    async def __aexit__(self, *a): return False
try:
    asyncio.run(switch.require_hardening(lambda: C()))
except switch.PreconditionError as e:
    print("precondition refuses without a database:", "could not be checked" in str(e))
else:
    raise SystemExit("the precondition did not refuse")' ) | grep -qx 'precondition refuses without a database: True' \
    || { echo "STOP: the 6a-4 precondition probe failed on the built release"; return 1; }
  [ "$(find "$REL_NEW" ! -user root | wc -l)" = 0 ] && [ "$(find "$REL_NEW" ! -type l -perm /g=w | wc -l)" = 0 ] \
    && [ "$(find "$REL_NEW" ! -type l -perm /o=rwx | wc -l)" = 0 ] && [ "$(find "$REL_NEW/backend" "$REL_NEW/database" -name __pycache__ | wc -l)" = 0 ] \
    || { echo "STOP: ownership / permissions / bytecode"; return 1; }
  echo "== B3 after-checks: nothing else moved"
  head_is "$PRE_HEAD" && [ "$(readlink -f "$APP_CURRENT")" = "$rel" ] && api_healthy "$old" >/dev/null && [ "$(env_sha)" = "$(facts ENV_SHA)" ] \
    || { echo "STOP: something moved during the build (only $REL_NEW was written; remove it only on approval)"; return 1; }
  date -u +%FT%TZ > "$ST/B-passed" && echo "B OK: release $NEW_ID built and probed; database $PRE_HEAD, $old still serving, nothing switched"
}
b_build
