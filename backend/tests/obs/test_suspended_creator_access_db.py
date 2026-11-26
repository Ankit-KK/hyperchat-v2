"""Audit T-4: a SUSPENDED creator's OBS token on the token endpoints (claim, overlay-config, leaderboard) and on Pusher
channel authorization. The audit expects all four refused. Today they are all served: the token lookup
(PostgresObsTokenStore.validate) never reads streamers.status, and suspension is documented as keeping "the
dashboard, OBS tokens and history" (AdminService.change_creator_status, owner decision 2026-10-15). The refusal
cases are strict xfails that lock today's behaviour until the owner decides; the active-creator control must pass."""

import pytest

from tests.integration import make_streamer, requires_db, update_streamer
from tests.obs.test_obs_db import BASE, obs_token, queued
from tests.realtime.test_realtime_db import auth, own, rt  # noqa: F401 (fixture: realtime enabled, TEST-ONLY Pusher)

pytestmark = requires_db
SLUG = "authtest-t4-susp"
FINDING = ("T-4 finding: a suspended creator's OBS token is still served (token validation ignores the creator's "
           "status; suspension keeps OBS tokens by the 2026-10-15 owner decision)")


def call(client, endpoint: str, token: str):
    hdr = {"x-obs-token": token}
    if endpoint == "claim":
        return client.post("/api/v1/obs/audio/claim", headers=hdr)
    if endpoint == "pusher-auth":
        return auth(client, own(f"private-{SLUG}-alerts", token), hdr)  # the token's own channel (NV-4)
    return client.get(f"/api/v1/obs/{endpoint}", headers=hdr)


@pytest.fixture
def creator(rt):  # noqa: F811
    rt.app.state.obs.public_base = BASE  # the scratch app has no R2; claims only need the public base
    sid = make_streamer(SLUG)
    queued(sid)  # something to claim, so a served claim is a 200, not an empty 204
    return rt, sid, obs_token(sid)


ENDPOINTS = ("claim", "overlay-config", "leaderboard", "pusher-auth")


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_an_active_creators_token_is_served(creator, endpoint):
    client, _, token = creator
    assert call(client, endpoint, token).status_code == 200


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.xfail(strict=True, raises=AssertionError, reason=FINDING)
def test_a_suspended_creators_token_is_refused(creator, endpoint):
    client, sid, token = creator
    update_streamer(sid, status="suspended")
    r = call(client, endpoint, token)
    assert r.status_code in (401, 403, 404), (endpoint, r.status_code)
