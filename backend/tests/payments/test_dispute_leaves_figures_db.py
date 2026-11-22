"""Audit T-5: while HYPERCHAT_PAYMENT_ACCOUNTING_ENABLED is off, a recorded dispute (migration 0029 evidence, through the
real webhook route) leaves every figure the API serves unchanged: donor totals, the public goal progress, the OBS
leaderboard and the dashboard stats. Locks today's behaviour; PAY-15 changes it only once accounting is switched on."""

from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from tests.integration import DOMAIN, login, make_streamer, make_user, requires_db, update_streamer
from tests.obs.test_obs_db import obs_token
from tests.payments.test_integrity_db import dispute, evidence
from tests.payments.test_payments_db import (  # noqa: F401 (fixtures)
    checkout,
    client,
    rz_env,
    totals,
    webhook,
)

pytestmark = requires_db
SLUG = "authtest-t5-disp"


def figures(client, sid: str, token: str) -> dict:
    goal = client.get(f"/api/v1/public/streamers/{SLUG}/goal")
    board = client.get("/api/v1/obs/leaderboard", headers={"x-obs-token": token})
    stats = client.get(f"/api/v1/dashboard/streamers/{SLUG}/donations/stats")
    assert (goal.status_code, board.status_code, stats.status_code) == (200, 200, 200), (goal.text, board.text,
                                                                                         stats.text)
    return {"totals": totals(sid), "goal": goal.json(), "leaderboard": board.json(),
            "stats": {k: v for k, v in stats.json().items() if k != "as_of"}}


def test_a_recorded_dispute_leaves_totals_goal_leaderboard_and_stats_unchanged(client):  # noqa: F811
    assert Settings().payment_accounting_enabled is False  # the switch this test is about
    email = "t5owner" + DOMAIN
    sid = make_streamer(SLUG, user_id=make_user(email))
    update_streamer(sid, goal_name="New mic", goal_target_amount=1000, goal_is_active=True,
                    goal_activated_at=datetime.now(UTC) - timedelta(hours=1), leaderboard_widget_enabled=True)
    assert login(client, email).status_code == 200
    token = obs_token(sid)
    out = checkout(client, SLUG)
    payment = client.fake.captured(out["razorpay_order_id"], 10000)
    assert webhook(client, "payment.captured", out, payment.id, event_id="authtest-cap-t5").json() == {
        "status": "completed"}

    before = figures(client, sid, token)
    # not vacuous: the payment is in every figure
    assert before["totals"] == [("Ravi", 100, 1)]
    assert before["goal"]["active"] is True and before["goal"]["progress_inr"] == 100
    assert before["leaderboard"] == {"enabled": True, "top_donor": {"name": "Ravi", "total_amount_inr": 100.0}}
    assert before["stats"]["total_revenue_inr"] == 100 and before["stats"]["total_donations"] == 1

    for n, event in enumerate(("payment.dispute.created", "payment.dispute.lost")):
        r = dispute(client, event, out, payment.id, f"authtest-t5-disp-{n}", ref="disp_TESTt5", amount_deducted=10000)
        assert r.json() == {"status": "integrity_recorded"}, r.text
    assert [e["status"] for e in evidence("authtest-t5-disp-")] == ["open", "lost"]

    assert figures(client, sid, token) == before
