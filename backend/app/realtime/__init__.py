"""Realtime (Phase 9): transactional outbox -> worker -> Pusher, plus private-channel authorization.

  outbox.py     emit(cur, ...) in the caller's transaction; claim / finish for the publisher
  channels.py   channel names (legacy: private-{slug}-{suffix}, public {slug}-goal) and parsing
  pusher.py     Pusher apps per streamers.pusher_group, REST signing, channel-auth signing, HTTP client
  payloads.py   per-event payloads rendered from the current database state (field allowlists)
  publisher.py  the worker loop (per-creator FIFO, retries, expiry)
  auth.py       POST /api/v1/realtime/pusher/auth and GET /api/v1/realtime/config
"""
