# S (6b-2 post, Stage 6 plan §5 steps 7-9): regression and reports on the switched DEV, switch OFF. No real payment.
#   S1 DEV smoke harness (pinned archive; strict head 0035; synthetic authtest-* rows, its own cleanup)
#   S2 payment-integrity-report (read-only) and the read-only verifier: recorded (pre-A5c findings expected, OD-6b-3)
#   S3 the L4 deploy-check dry run against the previous release (applied reversals: 0, so a rollback is allowed)
s_post() { local SDIR rc old rel l4 nok=0 nbad=0
  ok() { echo "OK    $1"; nok=$((nok+1)); }; bad() { echo "FAIL  $1"; nbad=$((nbad+1)); }
  [ -f "$ST/W-passed" ] && [ ! -e "$ST/S-passed" ] || { echo "STOP: W has not passed, or S already passed"; return 1; }
  old="$(facts OLD_ID)"; rel="$(facts OLD_REL)"
  window_ok 45 && [ "$(readlink -f "$APP_CURRENT")" = "$REL_NEW" ] && api_healthy "$NEW_ID" >/dev/null && target_ok && head_is "$TARGET_HEAD" \
    || { echo "STOP: DEV is not the W state"; return 1; }
  prestate_core > "$ST/S-prestate-before.txt"
  echo "== S1 DEV smoke harness (strict, head $TARGET_HEAD)"
  [ "$(sha256sum "$SMOKE_TAR" | cut -c1-64)" = "$SMOKE_SHA" ] || { echo "STOP: the smoke archive is not the pinned one"; return 1; }
  SDIR="$ST/smoke"; [ ! -e "$SDIR" ] && mkdir -m 700 "$SDIR" && tar -x -C "$SDIR" -f "$SMOKE_TAR" && ln -s "$REL_NEW/backend" "$SDIR/backend" \
    || { echo "STOP: cannot lay out the smoke checkout"; return 1; }
  grep -q '"0035"' "$SDIR/tests/e2e/smoke_policy.py" && ok "0035 is a known head of this smoke harness" || { bad "smoke harness does not know 0035"; return 1; }
  ( cd "$SDIR" && env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin PYTHONDONTWRITEBYTECODE=1 HYPERCHAT_SMOKE_EXPECT_HEAD="$TARGET_HEAD" \
      "$REL_NEW/venv/bin/python" -B -E -s tests/e2e/smoke_dev_backend.py ) > "$ST/smoke.out" 2>&1; rc=$?
  grep -E '^(FAIL|BLOCKED|== )' "$ST/smoke.out" | sed 's/^/   /'
  [ "$rc" = 0 ] && ok "smoke harness exit 0 (only the approved external BLOCKED remain)" || bad "smoke harness exit $rc (see smoke.out)"
  same S-prestate-before.txt prestate_core && ok "pre-state identical after the smoke (its synthetic rows cleaned)" || bad "smoke left rows behind (reported, NOT deleted)"
  echo "== S2 reports (read-only, recorded)"
  ( cd "$REL_NEW/backend" && "$REL_NEW/venv/bin/python" -B -E -s -m app.cli --env-file "$API_ENV" payment-integrity-report ) > "$ST/integrity-report.out" 2>&1; rc=$?
  echo "INFO  payment-integrity-report exit $rc (0 clean; 1 = pre-existing PAY-13 attention items, recorded for the owner)"
  [ "$rc" -le 1 ] && ok "integrity report ran (exit $rc)" || bad "integrity report exit $rc"
  [ -n "$(. "$ST/decisions.env"; echo "${OD_6B_READONLY_DSN:-}")" ] \
    && ( cd "$REL_NEW/backend" && env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 HYPERCHAT_READONLY_DSN="$(. "$ST/decisions.env"; echo "$OD_6B_READONLY_DSN")" \
         "$REL_NEW/venv/bin/python" -B -E -s -m app.payments.operator verify --max-seconds 300 > "$ST/S-verifier.json" 2>&1 ); rc=$?
  echo "INFO  verifier exit $rc (pre-A5c findings expected on DEV data: OD-6b-3)"
  echo "== S3 L4 deploy-check dry run (read-only)"
  # the release id exactly as the host names it (D-6b-1 fix, #141: the deployed release must contain it)
  ( cd "$REL_NEW/backend" && env -i PATH=/usr/bin:/bin HYPERCHAT_READONLY_DSN="$(. "$ST/decisions.env"; echo "${OD_6B_READONLY_DSN:-}")" \
      "$REL_NEW/venv/bin/python" -B -E -s -m app.payments.operator deploy-check --target-release "$old" \
      --releases-root "$RELEASES" ) > "$ST/l4.json" 2>&1; rc=$?
  [ "$rc" = 0 ] && grep -q '"carries_writer_rule":false' "$ST/l4.json" && grep -q '"applied_reversals":0' "$ST/l4.json" \
    && ok "L4: rollback to $old allowed (it lacks the writer, but no reversal is applied)" \
    || bad "L4 deploy-check exit $rc (see l4.json; 'not a 40-hex commit' = this release predates the #141 fix)"
  [ "$(find "$REL_NEW/backend" "$REL_NEW/database" "$SDIR" -name __pycache__ | wc -l)" = 0 ] && ok "no __pycache__" || bad "bytecode written"
  echo "== $nok OK, $nbad FAIL"
  [ "$nbad" = 0 ] && date -u +%FT%TZ > "$ST/S-passed" && echo "S OK: smoke, reports and L4 recorded; switch OFF"
  [ "$nbad" = 0 ]; }
s_post
