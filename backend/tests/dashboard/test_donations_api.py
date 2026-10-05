"""G2 donations: list (D-R2), stats (D-R3), CSV export (D-R4) against a scratch database."""

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest

from tests.integration import db, make_donation, make_streamer, owned_creator, requires_db

pytestmark = requires_db
S = "/api/v1/dashboard/streamers"
ITEM_FIELDS = {"donation_id", "donor_name", "amount", "currency", "amount_inr", "message", "message_visible",
               "is_hyperemote", "voice_message_url", "tts_audio_url", "hypersound_url", "media_url", "media_type",
               "moderation_status", "created_at"}


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ── D-R2 list ───────────────────────────────────────────────────────────────────

def test_list_shows_paid_approved_donations_newest_first(app):
    sid, _ = owned_creator(app, "authtest-list")
    other = make_streamer("authtest-list-other")
    t = _utc(2026, 10, 1, 12)
    auto = make_donation(sid, 100, moderation="auto_approved", at=t, name="Auto", message="hi")
    approved = make_donation(sid, 200, moderation="approved", at=t + timedelta(minutes=1), name="Appr",
                             message="hidden one", visible=False)
    for kwargs in ({"moderation": "pending"}, {"moderation": "rejected"}, {"payment": "pending", "moderation": "approved"},
                   {"payment": "failed", "moderation": "auto_approved"}):
        make_donation(sid, 7, at=t + timedelta(minutes=5), **kwargs)
    make_donation(other, 999, moderation="approved", at=t + timedelta(minutes=9))
    with db() as conn:
        conn.execute("UPDATE public.donations SET provider_order_id = 'authtest-rzp', status_token_hash = 'authtest-h', "
                     "is_hyperemote = true, media_url = 'https://cdn.example.test/m.gif', media_type = 'gif' "
                     "WHERE id = %s", (auto,))
    r = app.get(f"{S}/authtest-list/donations")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {"donations", "next_cursor", "has_more"} and body["has_more"] is False
    assert body["next_cursor"] is None
    assert [d["donation_id"] for d in body["donations"]] == [approved, auto]
    assert all(set(d) == ITEM_FIELDS for d in body["donations"])
    first, second = body["donations"]
    assert first["message"] == "hidden one" and first["message_visible"] is False  # hidden messages shown (D8)
    assert second == {"donation_id": auto, "donor_name": "Auto", "amount": 100.0, "currency": "INR",
                      "amount_inr": 100.0, "message": "hi", "message_visible": True, "is_hyperemote": True,
                      "voice_message_url": None, "tts_audio_url": None, "hypersound_url": None,
                      "media_url": "https://cdn.example.test/m.gif", "media_type": "gif",
                      "moderation_status": "auto_approved", "created_at": second["created_at"]}
    assert datetime.fromisoformat(second["created_at"]) == t
    assert "authtest-rzp" not in r.text and "authtest-h" not in r.text and "order" not in r.text


def test_pagination_walks_every_row_once_with_equal_timestamps(app):
    sid, _ = owned_creator(app, "authtest-pages")
    t = _utc(2026, 10, 1, 12)
    ids = [make_donation(sid, i + 1, moderation="approved", at=t if i < 4 else t - timedelta(seconds=i))
           for i in range(7)]
    expected = sorted(ids[:4], reverse=True) + ids[4:]  # ties broken by id DESC, then older rows
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 3} | ({"cursor": cursor} if cursor else {})
        body = app.get(f"{S}/authtest-pages/donations", params=params).json()
        seen += [d["donation_id"] for d in body["donations"]]
        pages += 1
        if not body["has_more"]:
            assert body["next_cursor"] is None
            break
        cursor = body["next_cursor"]
    assert seen == expected and pages == 3
    assert len(app.get(f"{S}/authtest-pages/donations").json()["donations"]) == 7  # default limit 20


@pytest.mark.parametrize("params,status", [({"limit": 0}, 422), ({"limit": 101}, 422), ({"limit": "x"}, 422),
                                           ({"cursor": "garbage"}, 400), ({"cursor": ""}, 400), ({"limit": 100}, 200)])
def test_list_parameter_validation(app, params, status):
    owned_creator(app, "authtest-params")
    r = app.get(f"{S}/authtest-params/donations", params=params)
    assert r.status_code == status and r.headers["cache-control"] == "no-store"
    if status == 400:
        assert r.json() == {"error": "Invalid cursor"}


# ── D-R3 stats (fixed clock, IST buckets) ───────────────────────────────────────

NOW = _utc(2026, 10, 5, 18, 30)  # = 2026-10-06 00:00:00 IST: a new IST day has just begun


def test_stats_bucket_paid_donations_by_ist_day_and_month(app):
    sid, _ = owned_creator(app, "authtest-stats")
    other = make_streamer("authtest-stats-other")
    app.app_state.dashboard.clock = lambda: NOW
    make_donation(sid, 1, moderation="pending", at=_utc(2026, 10, 5, 18, 30))                 # today (IST)
    make_donation(sid, 2, moderation="rejected", at=_utc(2026, 10, 5, 18, 31))                # today, any status
    make_donation(sid, 4, moderation="approved", at=_utc(2026, 10, 5, 18, 29, 59, 999999))    # yesterday, Oct
    make_donation(sid, 8, moderation="auto_approved", at=_utc(2026, 9, 30, 18, 30))            # 1 Oct 00:00 IST
    make_donation(sid, 16, moderation="approved", at=_utc(2026, 9, 30, 18, 29, 59))            # 30 Sep IST
    make_donation(sid, 32, payment="pending", at=_utc(2026, 10, 5, 19))                        # unpaid
    make_donation(sid, 64, payment="failed", at=_utc(2026, 10, 5, 19))                         # unpaid
    make_donation(other, 128, moderation="approved", at=_utc(2026, 10, 5, 19))                # other creator
    r = app.get(f"{S}/authtest-stats/donations/stats")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert datetime.fromisoformat(body.pop("as_of")) == NOW
    assert body == {"total_revenue_inr": 31.0, "total_donations": 5, "today_revenue_inr": 3.0,
                    "month_revenue_inr": 15.0, "timezone": "Asia/Kolkata"}
    app.app_state.dashboard.clock = lambda: NOW - timedelta(microseconds=1)  # 5 Oct 23:59:59.999999 IST
    body = app.get(f"{S}/authtest-stats/donations/stats").json()
    assert (body["today_revenue_inr"], body["month_revenue_inr"]) == (4.0, 15.0)


def test_stats_for_a_creator_without_donations(app):
    owned_creator(app, "authtest-stats-empty")
    body = app.get(f"{S}/authtest-stats-empty/donations/stats").json()
    assert {k: body[k] for k in ("total_revenue_inr", "total_donations", "today_revenue_inr",
                                 "month_revenue_inr")} == {"total_revenue_inr": 0.0, "total_donations": 0,
                                                           "today_revenue_inr": 0.0, "month_revenue_inr": 0.0}


# ── D-R4 CSV export ─────────────────────────────────────────────────────────────

def _export(app, slug, start, end):
    return app.get(f"{S}/{slug}/donations/export.csv", params={"from": start, "to": end})


def test_export_whole_ist_days_columns_and_formula_safety(app):
    sid, _ = owned_creator(app, "authtest-csv")
    other = make_streamer("authtest-csv-other")
    make_donation(sid, 1, at=_utc(2026, 9, 30, 18, 29, 59), name="Before")                 # 30 Sep IST
    make_donation(sid, 2, at=_utc(2026, 9, 30, 18, 30), name="=HYPERLINK(\"http://x\")")   # 1 Oct 00:00 IST
    make_donation(sid, 3, at=_utc(2026, 10, 31, 18, 29, 59), name="+Last", currency="USD", amount=0.05,
                  moderation="rejected")                                                    # 31 Oct 23:59:59 IST
    make_donation(sid, 4, at=_utc(2026, 10, 31, 18, 30), name="After")                     # 1 Nov IST
    make_donation(sid, 5, at=_utc(2026, 10, 10), payment="pending", name="Unpaid")
    make_donation(other, 6, at=_utc(2026, 10, 10), name="Other creator")
    r = _export(app, "authtest-csv", "2026-10-01", "2026-10-31")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["content-disposition"] == 'attachment; filename="authtest-csv-donations-2026-10-01-2026-10-31.csv"'
    assert list(csv.reader(io.StringIO(r.text))) == [   # newest first; any moderation status; IST timestamps
        ["created_at", "donor_name", "amount", "currency", "amount_inr"],
        ["2026-10-31T23:59:59+05:30", "'+Last", "0.05", "USD", "3.00"],
        ["2026-10-01T00:00:00+05:30", "'=HYPERLINK(\"http://x\")", "2", "INR", "2.00"],
    ]
    with db() as conn:
        order_ids = [o for (o,) in conn.execute("SELECT order_id FROM public.donations WHERE streamer_id = %s", (sid,))]
    assert not any(o in r.text for o in order_ids) and "order" not in r.text


@pytest.mark.parametrize("start,end,error", [
    (None, "2026-10-01", "from is required (YYYY-MM-DD)"),
    ("2026-10-01", None, "to is required (YYYY-MM-DD)"),
    ("01-10-2026", "2026-10-02", "from must be a date in YYYY-MM-DD format"),
    ("2026-10-02", "2026-10-01", "to must not be before from"),
    ("2025-01-01", "2026-01-02", "The date range is limited to 366 days"),
])
def test_export_rejects_bad_ranges(app, start, end, error):
    owned_creator(app, "authtest-csv-bad")
    params = {k: val for k, val in (("from", start), ("to", end)) if val is not None}
    r = app.get(f"{S}/authtest-csv-bad/donations/export.csv", params=params)
    assert (r.status_code, r.json()) == (400, {"error": error}) and r.headers["cache-control"] == "no-store"


def test_export_366_days_is_allowed(app):
    owned_creator(app, "authtest-csv-year")
    assert _export(app, "authtest-csv-year", "2025-01-01", "2026-01-01").status_code == 200


def test_export_over_10000_rows_is_refused_not_truncated(app):
    sid, _ = owned_creator(app, "authtest-csv-big")
    with db() as conn:
        conn.execute("""
            INSERT INTO public.donations (streamer_id, order_id, donor_name, amount, amount_inr, payment_status,
                                          created_at)
            SELECT %s, 'authtest-bulk-' || g, 'Bulk ' || g, 1, 1, 'success', %s::timestamptz + g * interval '1 second'
            FROM generate_series(1, 10000) AS g
        """, (sid, _utc(2026, 10, 1)))
    ok = _export(app, "authtest-csv-big", "2026-10-01", "2026-10-02")
    assert ok.status_code == 200 and len(list(csv.reader(io.StringIO(ok.text)))) == 10_001
    make_donation(sid, 1, at=_utc(2026, 10, 1, 20))
    r = _export(app, "authtest-csv-big", "2026-10-01", "2026-10-02")
    assert (r.status_code, r.json()) == (400, {"error": "More than 10000 donations in this range: choose a shorter range"})
    assert _export(app, "authtest-csv-big", "2026-10-02", "2026-10-02").status_code == 200  # a narrower range works


def test_export_is_audited_with_a_count(app):
    sid, _ = owned_creator(app, "authtest-csv-audit")
    make_donation(sid, 1, at=_utc(2026, 10, 1, 12))
    _export(app, "authtest-csv-audit", "2026-10-01", "2026-10-01")
    assert any("donations_exported" in rec and sid in rec and "'count': 1" in rec
               for rec in app.app_state.log_capture.records)
