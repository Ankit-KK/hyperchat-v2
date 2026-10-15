"""Reconciliation between the restored source and the loaded target. Counts and sums only."""

from collections import Counter

import psycopg
from psycopg import sql


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


def reconcile(src: psycopg.Connection, tgt: psycopg.Connection, stats: dict, target_db: str, *,
              legacy_media_bases: tuple[str, ...] = (), media_rewritten: dict | None = None) -> tuple[bool, str]:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

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

    # 2. Donations per streamer: count, sum(amount), sum(amount_inr) identical for all source tables.
    union = _donation_union(src, "amount, amount_inr")
    source_per = {r[0]: r[1:] for r in _all(src, f"SELECT src, count(*), sum(amount), sum(amount_inr) "
                                                 f"FROM ({union}) d GROUP BY src")}
    target_per = {r[0]: r[1:] for r in _all(tgt, "SELECT legacy_source_table, count(*), sum(amount), "
                                                 "sum(amount_inr) FROM public.donations GROUP BY 1")}
    mismatched = [t for t in source_per if source_per[t] != target_per.get(t)]
    check("donations: per-streamer count/sum(amount)/sum(amount_inr)", not mismatched and
          len(source_per) == len(target_per), f"{len(source_per)} streamers compared, {len(mismatched)} mismatched")

    # 3. Value distributions.
    for col_src, col_tgt in (("payment_status", "payment_status"), ("moderation_status", "moderation_status"),
                             ("currency::text", "currency"), ("media_type", "media_type"),
                             ("is_hyperemote", "is_hyperemote"), ("message_visible", "message_visible"),
                             ("mod_notified", "mod_notified")):
        u = _donation_union(src, f"{col_src} AS v")
        a = Counter(dict(_all(src, f"SELECT v::text, count(*) FROM ({u}) d GROUP BY 1")))
        b = Counter(dict(_all(tgt, f"SELECT {col_tgt}::text, count(*) FROM public.donations GROUP BY 1")))
        check(f"donations: distribution of {col_tgt}", a == b, f"{len(a)} distinct values")

    # 4. Media/audio presence.
    u = _donation_union(src, "voice_message_url, hypersound_url, media_url, tts_audio_url, audio_played_at, "
                             "audio_scheduled_at, status_token_hash, upload_token_hash, message")
    cols = ("voice_message_url", "hypersound_url", "media_url", "tts_audio_url", "audio_played_at",
            "audio_scheduled_at", "status_token_hash", "upload_token_hash", "message")
    counts = ", ".join(f"count({c})" for c in cols)
    a = _all(src, f"SELECT {counts} FROM ({u}) d")[0]
    b = _all(tgt, f"SELECT {counts} FROM public.donations")[0]
    check("donations: non-null counts of media/audio/token/message columns", a == b, str(dict(zip(cols, b))))

    # 5. approved_by split covers every source value.
    u = _donation_union(src, "approved_by::text AS a")
    n_src = _all(src, f"SELECT count(a) FROM ({u}) d")[0][0]
    n_tgt = _all(tgt, "SELECT count(approved_by_moderator_id) + count(approved_by_label) FROM public.donations")[0][0]
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
    flags = _all(src, "SELECT count(*) FILTER (WHERE media_enabled), count(*) FILTER (WHERE tts_enabled) "
                      "FROM public.sapphire_donations")[0]
    check("dropped columns were empty", sum(empty_checks) == 0 and flags == (0, 0),
          f"{len(empty_checks)} column instances checked")
    # sapphire.status is dropped because it was always the default 'pending' and no code ever wrote it
    # (payment_status is authoritative). If any other value (or NULL) appears before cutover, the
    # column has started carrying information and the drop must be re-evaluated: fail the run.
    values, other = _all(src, "SELECT array_agg(DISTINCT status ORDER BY status), "
                              "count(*) FILTER (WHERE status IS DISTINCT FROM 'pending') "
                              "FROM public.sapphire_donations")[0]
    check("sapphire.status has exactly one distinct value: 'pending'", values == ["pending"] and other == 0,
          f"distinct={values} rows_not_pending={other}")

    # 7. Other invariants.
    dup = _all(tgt, "SELECT count(*) FROM (SELECT provider_order_id FROM public.donations WHERE provider_order_id "
                    "IS NOT NULL GROUP BY 1 HAVING count(*) > 1) x")[0][0]
    check("donations: duplicate provider_order_id groups (informational)", True, str(dup))
    bad_hash = _all(tgt, "SELECT count(*) FROM public.obs_tokens WHERE token_hash !~ '^[0-9a-f]{64}$'")[0][0]
    check("obs_tokens: every token stored as SHA-256", bad_hash == 0, f"invalid={bad_hash}")
    pw = _all(tgt, "SELECT count(*) FILTER (WHERE password_hash IS NOT NULL) FROM public.users")[0][0]
    check("users: no password hashes migrated (cutover import)", pw == 0, f"with_password={pw}")
    roles = dict(_all(tgt, "SELECT role, count(*) FROM public.users GROUP BY 1"))
    check("users: role distribution (admin_emails folded in)", True, str(roles))
    s_tot = _all(src, "SELECT sum(total_amount), sum(donation_count) FROM public.streamer_donator_totals")[0]
    t_tot = _all(tgt, "SELECT sum(total_amount), sum(donation_count) FROM public.streamer_donator_totals")[0]
    check("streamer_donator_totals: sums", s_tot == t_tot, "")
    disabled = _all(tgt, "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                         "WHERE c.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal AND t.tgenabled <> 'O'")[0][0]
    check("target: all user triggers re-enabled", disabled == 0, f"disabled={disabled}")

    # 8. Media URLs (D9): after a rewrite no loaded media URL may still use a legacy base.
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

    ok = all(c[1] for c in checks)
    lines = [f"ETL REHEARSAL RECONCILIATION  target={target_db}  result={'PASS' if ok else 'FAIL'}", ""]
    lines += [f"[{'PASS' if c[1] else 'FAIL'}] {c[0]}" + (f"  -- {c[2]}" if c[2] else "") for c in checks]
    return ok, "\n".join(lines) + "\n"
