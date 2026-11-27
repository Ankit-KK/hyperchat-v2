"""Reconciliation between the restored source and the loaded target. Counts and sums only."""

import sys
from collections import Counter
from pathlib import Path

import psycopg
from psycopg import sql

VERIFY_SECONDS = 600  # the verifier's run budget (it requires one; no production default exists)
_PAID = "CASE WHEN payment_status = 'success' THEN 'success' ELSE 'failed' END"  # NV3-1: unpaid rows load 'failed'


def _all(conn: psycopg.Connection, query: str, params: tuple | None = None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(query, params or None)  # no params -> '%' is not a placeholder
        return cur.fetchall()


def _donation_union(src: psycopg.Connection, select_list: str) -> str:
    tables = [r[0] for r in _all(src, "SELECT table_name FROM information_schema.tables WHERE table_schema = "
                                      "'public' AND table_type = 'BASE TABLE' AND table_name LIKE '%\\_donations'")]
    return " UNION ALL ".join(
        sql.SQL("SELECT {} AS src, {} FROM {}").format(sql.Literal(t), sql.SQL(select_list),
                                                       sql.Identifier("public", t)).as_string(src)
        for t in sorted(tables))


def verifier_result(report: dict, i10_confirmed: bool) -> tuple[bool, str]:
    """The PAY-15 verifier on the loaded database: clean, or (until Phase 2B pins the I10 baseline) exactly the one
    I10 baseline_unverified finding every database at head reports. Counts only, never finding ids."""
    findings = report.get("findings", [])
    by = Counter((f["invariant"], f["class"], f.get("detail", {}).get("problem", "")) for f in findings)
    explained = [f for f in findings if f["invariant"] == "I10" and f.get("detail", {}).get("problem")
                 == "baseline_unverified"]
    if report.get("status") == "clean" and not findings:
        return True, "clean"
    ok = not i10_confirmed and len(explained) == 1 and len(findings) == 1
    detail = ", ".join(f"{i}/{c}/{p or '-'}={n}" for (i, c, p), n in sorted(by.items())) or "none"
    why = " (exactly the pre-Phase-2B I10 baseline_unverified finding)" if ok else ""
    return ok, f"status={report.get('status')} findings: {detail}{why}"


def run_verifier(dsn: str) -> tuple[bool, str]:
    """app.payments.verify as hyperchat_readonly on its own autocommit connection (it opens its own snapshot)."""
    backend = str(Path(__file__).resolve().parents[2] / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from app.payments import verify  # lazy: only the reconciliation needs the backend

    with psycopg.connect(dsn, autocommit=True) as conn:
        report = verify.verify(conn, max_seconds=VERIFY_SECONDS)
    return verifier_result(report, getattr(verify, "I10_BASELINE_CONFIRMED", False))


def reconcile(src: psycopg.Connection, tgt: psycopg.Connection, stats: dict, target_db: str, *,
              legacy_media_bases: tuple[str, ...] = (), media_rewritten: dict | None = None,
              credit_backfill: dict | None = None, verify_dsn: str | None = None) -> tuple[bool, str]:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    def one(conn, query):
        return _all(conn, query)[0]

    # 1. Every source row is either loaded or skipped with a reason; target counts match what was loaded.
    target_tables = {r[0] for r in _all(tgt, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    for table, s in sorted(stats.items()):
        skipped = sum(s["skipped"].values())
        target_n = None
        if table in target_tables:
            target_n = _all(tgt, sql.SQL("SELECT count(*) FROM {}").format(
                sql.Identifier("public", table)).as_string(tgt))[0][0]
        accounted = s["loaded"] + skipped == s["source"]
        in_target = target_n is None or target_n == s["loaded"]
        detail = f"source={s['source']} loaded={s['loaded']}" + (
            f" skipped={s['skipped']}" if s["skipped"] else "") + (f" ({s['reason']})" if s.get("reason") else "")
        check(f"rows: {table}", accounted and in_target, detail)

    # 2. Donations per creator: count, sum(amount), sum(amount_inr), by source table AND by creator id, and per
    #    payment status.
    union = _donation_union(src, "streamer_id, amount, amount_inr, payment_status")
    source_per = {r[0]: r[1:] for r in _all(src, f"SELECT src, count(*), sum(amount), sum(amount_inr) "
                                                 f"FROM ({union}) d GROUP BY src")}
    target_per = {r[0]: r[1:] for r in _all(tgt, "SELECT legacy_source_table, count(*), sum(amount), "
                                                 "sum(amount_inr) FROM public.donations GROUP BY 1")}
    mismatched = [t for t in source_per if source_per[t] != target_per.get(t)]
    check("donations: per-source-table count/sum(amount)/sum(amount_inr)", not mismatched and
          len(source_per) == len(target_per), f"{len(source_per)} tables compared, {len(mismatched)} mismatched")
    s_creator = dict((r[0], r[1:]) for r in _all(src, f"SELECT streamer_id::text, count(*), sum(amount), "
                                                      f"sum(amount_inr) FROM ({union}) d GROUP BY 1"))
    t_creator = dict((r[0], r[1:]) for r in _all(tgt, "SELECT streamer_id::text, count(*), sum(amount), "
                                                      "sum(amount_inr) FROM public.donations GROUP BY 1"))
    check("donations: per-creator count/sum(amount)/sum(amount_inr)", s_creator == t_creator,
          f"{len(s_creator)} creators compared, "
          f"{sum(1 for k in s_creator.keys() | t_creator.keys() if s_creator.get(k) != t_creator.get(k))} mismatched")
    # NV3-1: every unpaid legacy row (pending / failed) loads as terminal 'failed'
    s_pay = dict((r[0], r[1:]) for r in _all(src, f"SELECT {_PAID}, count(*), sum(amount_inr) "
                                                  f"FROM ({union}) d GROUP BY 1"))
    t_pay = dict((r[0], r[1:]) for r in _all(tgt, "SELECT payment_status, count(*), sum(amount_inr) "
                                                  "FROM public.donations GROUP BY 1"))
    check("donations: count/sum(amount_inr) per payment status (unpaid legacy rows loaded 'failed')", s_pay == t_pay,
          ", ".join(f"{k}={v[0]}/{v[1]}" for k, v in sorted(t_pay.items())))

    # 3. Value distributions (media_type is cleared: section 4).
    for col_src, col_tgt in ((_PAID, "payment_status"), ("moderation_status", "moderation_status"),
                             ("currency::text", "currency"), ("is_hyperemote", "is_hyperemote"),
                             ("message_visible", "message_visible"), ("mod_notified", "mod_notified")):
        u = _donation_union(src, f"{col_src} AS v")
        a = Counter(dict(_all(src, f"SELECT v::text, count(*) FROM ({u}) d GROUP BY 1")))
        b = Counter(dict(_all(tgt, f"SELECT {col_tgt}::text, count(*) FROM public.donations GROUP BY 1")))
        check(f"donations: distribution of {col_tgt}", a == b, f"{len(a)} distinct values")

    # 4. Fresh start: no legacy media, voice message, donor link or OBS token survives; history columns are kept.
    from etl.mapping import DONATION_CLEARED_COLUMNS
    cleared = ", ".join(f"count({c})" for c in DONATION_CLEARED_COLUMNS)
    u = _donation_union(src, ", ".join(DONATION_CLEARED_COLUMNS))
    src_counts = dict(zip(DONATION_CLEARED_COLUMNS, one(src, f"SELECT {cleared} FROM ({u}) d")))
    tgt_counts = one(tgt, f"SELECT {cleared} FROM public.donations")
    loaded_cleared = stats.get("donations", {}).get("cleared_non_null", {})
    check("donations: legacy media / voice / donor-link columns are all NULL (fresh start)",
          not any(tgt_counts) and {k: v for k, v in src_counts.items() if v} == loaded_cleared,
          f"source non-null={src_counts}")
    kept = ("audio_played_at", "audio_scheduled_at", "message")
    u = _donation_union(src, ", ".join(kept))
    counts = ", ".join(f"count({c})" for c in kept)
    a, b = one(src, f"SELECT {counts} FROM ({u}) d"), one(tgt, f"SELECT {counts} FROM public.donations")
    check("donations: non-null counts of the kept history columns", a == b, str(dict(zip(kept, b))))
    left = one(tgt, "SELECT (SELECT count(*) FROM public.obs_tokens), (SELECT count(*) FROM "
                    "public.platform_soundboard_slots), (SELECT count(*) FROM public.streamer_soundboard_slots), "
                    "(SELECT count(*) FROM public.api_keys), (SELECT count(*) FROM public.partner_api_keys), "
                    "(SELECT count(*) FROM public.creator_image_uploads)")
    # 0039: legacy logos / backgrounds are copied by the asset plan WITHOUT an image record ("validity is the row;
    # legacy copies get no row"), so the fresh-start load leaves creator_image_uploads empty
    check("fresh start: no OBS token, soundboard slot, API key or image record in the target", not any(left),
          f"obs_tokens={left[0]} soundboard_slots={left[1] + left[2]} api_keys={left[3]} partner_api_keys={left[4]} "
          f"creator_image_uploads={left[5]}")

    # NV3-1: no unpaid legacy order can be completed in V2: none is non-terminal, none keeps a provider order id,
    # none has an order_lookup row.
    u = _donation_union(src, "payment_status")
    src_unpaid = dict(_all(src, f"SELECT payment_status, count(*) FROM ({u}) d WHERE payment_status <> 'success' "
                                f"GROUP BY 1 ORDER BY 1"))
    open_rows, with_order, lookups, failed = one(tgt, """
        SELECT count(*) FILTER (WHERE payment_status NOT IN ('success', 'failed')),
               count(*) FILTER (WHERE payment_status <> 'success' AND provider_order_id IS NOT NULL),
               (SELECT count(*) FROM public.order_lookup o JOIN public.donations x ON x.id = o.donation_id
                WHERE x.payment_status <> 'success'),
               count(*) FILTER (WHERE payment_status = 'failed')
        FROM public.donations WHERE legacy_source_table IS NOT NULL""")
    loaded_unpaid = stats.get("donations", {}).get("unpaid_loaded_failed", {})
    check("donations: no unpaid legacy order can be completed (terminal 'failed', no order id, no lookup)",
          open_rows == 0 and with_order == 0 and lookups == 0 and loaded_unpaid == src_unpaid
          and failed == sum(src_unpaid.values()),
          f"source unpaid={src_unpaid}; loaded failed={failed}; non-terminal={open_rows} with_order_id={with_order} "
          f"lookups={lookups}")

    # 5. approved_by split covers every source value.
    u = _donation_union(src, "approved_by::text AS a")
    n_src = one(src, f"SELECT count(a) FROM ({u}) d")[0]
    n_tgt = one(tgt, "SELECT count(approved_by_moderator_id) + count(approved_by_label) FROM public.donations")[0]
    check("donations: approved_by -> moderator_id | label", n_src == n_tgt,
          f"source non-null={n_src}, split={stats['donations'].get('approved_by_split')}")

    # 6. Dropped columns really were empty, and sapphire.status really is the never-written default.
    empty_checks = []
    for table, column in _all(src, "SELECT table_name, column_name FROM information_schema.columns "
                                   "WHERE table_schema = 'public' AND table_name LIKE '%\\_donations' AND column_name "
                                   "IN ('temp_voice_data', 'selected_gif_id', 'email', 'payment_id', 'signature')"):
        n = _all(src, sql.SQL("SELECT count({}) FROM {}").format(
            sql.Identifier(column), sql.Identifier("public", table)).as_string(src))[0][0]
        empty_checks.append(n)
    flags = one(src, "SELECT count(*) FILTER (WHERE media_enabled), count(*) FILTER (WHERE tts_enabled) "
                     "FROM public.sapphire_donations")
    check("dropped columns were empty", sum(empty_checks) == 0 and flags == (0, 0),
          f"{len(empty_checks)} column instances checked")
    # sapphire.status is dropped because it was always the default 'pending' and no code ever wrote it
    # (payment_status is authoritative). If any other value (or NULL) appears before cutover, the
    # column has started carrying information and the drop must be re-evaluated: fail the run.
    values, other = one(src, "SELECT array_agg(DISTINCT status ORDER BY status), "
                             "count(*) FILTER (WHERE status IS DISTINCT FROM 'pending') FROM public.sapphire_donations")
    check("sapphire.status has exactly one distinct value: 'pending'", values == ["pending"] and other == 0,
          f"distinct={values} rows_not_pending={other}")

    # 7. Donor totals are copied verbatim; PAY-15 (0031) credits are capped by them.
    s_tot = one(src, "SELECT count(*), sum(total_amount), sum(donation_count) FROM public.streamer_donator_totals")
    t_tot = one(tgt, "SELECT count(*), sum(total_amount), sum(donation_count) FROM public.streamer_donator_totals")
    check("streamer_donator_totals: rows and sums unchanged", s_tot == t_tot, f"rows={t_tot[0]}")
    s_bucket = dict((r[0], r[1]) for r in _all(src, "SELECT s.id::text || ':' || t.donator_name_lower, t.total_amount "
                                                    "FROM public.streamer_donator_totals t JOIN public.streamers s "
                                                    "ON s.streamer_slug = t.streamer_slug"))
    t_bucket = dict((r[0], r[1]) for r in _all(tgt, "SELECT streamer_id::text || ':' || donator_name_lower, "
                                                    "total_amount FROM public.streamer_donator_totals"))
    check("streamer_donator_totals: every bucket's total unchanged", s_bucket == t_bucket,
          f"{len(t_bucket)} buckets")
    over, short, short_inr, uncredited = one(tgt, """
        SELECT count(*) FILTER (WHERE b.credited > COALESCE(t.total_amount, 0)),
               count(*) FILTER (WHERE b.paid_approved > b.credited),
               COALESCE(sum(b.paid_approved - b.credited), 0),
               count(*) FILTER (WHERE b.credited < COALESCE(t.total_amount, 0) AND b.paid_approved > b.credited)
        FROM (SELECT streamer_id, lower(donor_name) AS name, sum(credited_inr) AS credited,
                     sum(amount_inr) FILTER (WHERE payment_status = 'success'
                                             AND moderation_status IN ('approved', 'auto_approved')) AS paid_approved
              FROM public.donations GROUP BY 1, 2) b
        LEFT JOIN public.streamer_donator_totals t ON t.streamer_id = b.streamer_id AND t.donator_name_lower = b.name""")
    check("donor totals vs donations: no bucket credits more than its total", over == 0 and uncredited == 0,
          f"over={over}; short buckets={short} (newest rows partly or not credited, {short_inr} INR)")
    bad, events, ev_sum, credited_sum, other_rows = one(tgt, """
        SELECT (SELECT count(*) FROM public.donations WHERE credited_inr <> COALESCE(creditable_inr, 0)
                   OR (credited_inr > 0 AND NOT (payment_status = 'success'
                                                 AND moderation_status IN ('approved', 'auto_approved')))),
               (SELECT count(*) FROM public.donation_credit_events WHERE cause = 'backfill'),
               (SELECT COALESCE(sum(delta_inr), 0) FROM public.donation_credit_events),
               (SELECT COALESCE(sum(credited_inr), 0) FROM public.donations),
               (SELECT count(*) FROM public.donation_credit_events WHERE cause <> 'backfill')
                 + (SELECT count(*) FROM public.donation_reversals)
                 + (SELECT count(*) FROM public.donation_accounting_evaluations)
                 + (SELECT count(*) FROM public.payment_accounting_events)
                 + (SELECT count(*) FROM public.payment_accounting_runs)""")
    backfilled = (credit_backfill or {}).get("credited")
    check("PAY-15 credits: one backfill event per credited donation, audit sum = credited sum, nothing else",
          bad == 0 and events == backfilled and ev_sum == credited_sum and other_rows == 0,
          f"credited={events} sum={credited_sum} INR; backfill={credit_backfill}")
    state = _all(tgt, "SELECT state, dispute_mode FROM public.payment_accounting_state")
    check("PAY-15 accounting state is off (imported donations are never reversed automatically)",
          [s[0] for s in state] == ["off"], str(state))

    # 8. Other invariants.
    dup = one(tgt, "SELECT count(*) FROM (SELECT provider_order_id FROM public.donations WHERE provider_order_id "
                   "IS NOT NULL GROUP BY 1 HAVING count(*) > 1) x")[0]
    check("donations: duplicate provider_order_id groups (informational)", True, str(dup))
    pw = one(tgt, "SELECT count(*) FILTER (WHERE password_hash IS NOT NULL) FROM public.users")[0]
    check("users: no password hashes migrated (cutover import)", pw == 0, f"with_password={pw}")
    roles = dict(_all(tgt, "SELECT role, count(*) FROM public.users GROUP BY 1"))
    # NV3-2: the V2 admins are exactly the accounts admin_emails lists; any other legacy role 'admin' is demoted
    listed = {r[0] for r in _all(src, "SELECT u.id::text FROM public.auth_users u WHERE lower(u.email) IN "
                                      "(SELECT lower(email) FROM public.admin_emails)")}
    admins = {r[0] for r in _all(tgt, "SELECT id::text FROM public.users WHERE role = 'admin'")}
    should_demote = one(src, "SELECT count(*) FROM public.auth_users WHERE role = 'admin' AND lower(email) NOT IN "
                             "(SELECT lower(email) FROM public.admin_emails)")[0]
    demoted = stats.get("users", {}).get("legacy_admin_role_demoted")
    check("users: admins are exactly the admin_emails accounts (legacy role 'admin' elsewhere demoted)",
          admins == listed and demoted == should_demote,
          f"admins={len(admins)} admin_emails accounts={len(listed)} demoted={demoted}; roles={roles}")
    # NV4: users.streamer_id links by kind. An owner link (the creator's streamers.user_id names the account) or a
    # linked non-owner account (owner decision 2026-11-25: linked accounts keep full power): reported, not a failure;
    # every link must still name a loaded creator, and the copy must equal the source.
    links = one(tgt, """
        SELECT count(*) FILTER (WHERE s.user_id = u.id), count(*) FILTER (WHERE s.user_id IS DISTINCT FROM u.id),
               count(*) FILTER (WHERE s.id IS NULL),
               (SELECT count(*) FROM public.streamers x WHERE x.user_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM public.users y WHERE y.id = x.user_id AND y.streamer_id = x.id))
        FROM public.users u LEFT JOIN public.streamers s ON s.id = u.streamer_id WHERE u.streamer_id IS NOT NULL""")
    src_links = one(src, "SELECT count(*) FROM public.auth_users WHERE streamer_id IS NOT NULL")[0]
    check("users: creator links (users.streamer_id) by kind (reported)",
          links[2] == 0 and links[0] + links[1] == src_links,
          f"owner={links[0]} linked_non_owner={links[1]} dangling={links[2]} source_links={src_links}; "
          f"owners_without_their_link={links[3]}")
    kyc = dict(_all(tgt, "SELECT kyc_track || '/' || status || '/' || CASE WHEN user_id IS NULL THEN 'no_owner' "
                         "WHEN user_id = kyc_baseline_owner_id THEN 'baseline_owner' ELSE 'other' END, count(*) "
                         "FROM public.streamers GROUP BY 1"))
    check("streamers: every creator on the legacy KYC track with its owner as baseline",
          all(k.startswith("legacy/") and not k.endswith("/other") and k != "legacy/active/no_owner" for k in kyc),
          str(kyc))
    s_status = Counter(dict(_all(src, "SELECT CASE WHEN user_id IS NULL AND status = 'active' THEN 'pending' "
                                      "ELSE status END, count(*) FROM public.streamers GROUP BY 1")))
    t_status = Counter(dict(_all(tgt, "SELECT status, count(*) FROM public.streamers GROUP BY 1")))
    moved = stats.get("streamers", {}).get("ownerless_active_loaded_pending")
    check("streamers: status kept, except active creators without an owner loaded 'pending'", s_status == t_status,
          f"loaded pending for no owner={moved}; status={dict(t_status)}")
    # Owner decision 2026-11-27: at launch KYC enforcement is switched to 'active' with NO grace grants. What each
    # creator's new donations would answer then (the 0023 rule, evaluated for state 'active'; nothing is written).
    launch = Counter(dict(_all(tgt, "SELECT COALESCE(public.kyc_donation_decision(id, 'active'), 'eligible'), "
                                    "count(*) FROM public.streamers GROUP BY 1")))
    grants = one(tgt, "SELECT count(*) FROM public.kyc_grace_grants")[0]
    check("KYC at launch (enforcement active, no grace): no migrated creator takes donations before KYC",
          launch["eligible"] == 0 and grants == 0,
          f"kyc_required={launch['kyc_required']} {dict(sorted(launch.items()))} grace_grants={grants}")
    disabled = one(tgt, "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                        "WHERE c.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal AND t.tgenabled <> 'O'")[0]
    check("target: all user triggers re-enabled", disabled == 0, f"disabled={disabled}")

    # 9. Media URLs (D9): after a rewrite no loaded creator image URL may still use a legacy base.
    if legacy_media_bases:
        from etl.mapping import MEDIA_URL_COLUMNS
        left = 0
        for table, columns in MEDIA_URL_COLUMNS.items():
            for column in columns:
                for base in legacy_media_bases:
                    left += _all(tgt, sql.SQL("SELECT count(*) FROM {} WHERE starts_with({}, {})").format(
                        sql.Identifier("public", table), sql.Identifier(column),
                        sql.Literal(base + "/")).as_string(tgt))[0][0]
        check("media URLs: none left on a legacy base", left == 0,
              f"left={left} rewritten={media_rewritten or {}}")

    # 10. The PAY-15 verifier (app.payments.verify) on the loaded database.
    if verify_dsn is None:
        check("PAY-15 verifier", False, "not run: no --verify-dsn")
    else:
        try:
            ok, detail = run_verifier(verify_dsn)
        except Exception as exc:  # noqa: BLE001 -- reported as a failed check, never a pass
            ok, detail = False, f"could not run: {type(exc).__name__}"
        check("PAY-15 verifier on the loaded database", ok, detail)

    ok = all(c[1] for c in checks)
    lines = [f"ETL RECONCILIATION  target={target_db}  result={'PASS' if ok else 'FAIL'}", ""]
    lines += [f"[{'PASS' if c[1] else 'FAIL'}] {c[0]}" + (f"  -- {c[2]}" if c[2] else "") for c in checks]
    return ok, "\n".join(lines) + "\n"
