"""Audit T-7: OBS playback is at-most-once by design. The claim marks audio_played_at in its own transaction; the player
has no way to report a failed playback (player.js: on an audio error or a refused play() it just schedules the next
claim). So after a claim, a failed playback is modelled as that next claim: it never returns the item again, and the
item stays marked played. Only the dashboard replay re-queues it (tests/obs/test_obs_db.py)."""

import pytest

from tests.integration import make_streamer, requires_db
from tests.obs.test_obs_db import BASE, obs, obs_token, played, queued  # noqa: F401 (fixture: the real app, OBS on)

pytestmark = requires_db
CLAIM = "/api/v1/obs/audio/claim"


def test_a_failed_playback_after_a_claim_does_not_requeue_the_item(obs):  # noqa: F811
    sid = make_streamer("authtest-t7-fail")
    hdr = {"x-obs-token": obs_token(sid)}
    did = queued(sid)
    r = obs.post(CLAIM, headers=hdr)
    assert r.status_code == 200 and r.json()["donation_id"] == did
    marked = played(did)
    assert marked is not None
    # the player's audio failed: it claims again (after 3 s), and again on its idle poll
    for _ in range(2):
        again = obs.post(CLAIM, headers=hdr)
        assert again.status_code == 204, again.text
    nxt = obs.get("/api/v1/obs/audio/next", params={"token": hdr["x-obs-token"]}, follow_redirects=False)
    assert nxt.status_code == 204  # the Media Source path neither
    assert played(did) == marked  # still played, unchanged


@pytest.mark.parametrize("path", ["/api/v1/obs/audio/failed", "/api/v1/obs/audio/error", "/api/v1/obs/audio/requeue"])
def test_there_is_no_endpoint_to_report_a_failed_playback(obs, path):  # noqa: F811
    sid = make_streamer("authtest-t7-none")
    hdr = {"x-obs-token": obs_token(sid)}
    did = queued(sid)
    obs.post(CLAIM, headers=hdr)
    assert obs.post(path, headers=hdr, json={"donation_id": did}).status_code in (404, 405)
    assert obs.post(CLAIM, headers=hdr).status_code == 204 and played(did) is not None
