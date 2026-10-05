"""Authorization: who may act on which creator (streamer).

Rules (same semantics as the legacy assertStreamerAccess):
  * an admin (users.role = 'admin') may act on every creator;
  * a creator nobody has claimed yet (streamers.user_id IS NULL) is admin-only;
  * otherwise the user must own it: streamers.user_id = users.id ("owner"), or the user's account is
    linked to it through users.streamer_id ("linked").
Authentication (who the caller is) stays in app.auth; this package only decides access.
"""
