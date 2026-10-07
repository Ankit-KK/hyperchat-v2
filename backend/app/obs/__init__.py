"""OBS audio playback (Phase 10).

  media.py       which URL a claimed donation plays (allow-listed objects of this environment's bucket only)
  tokens.py      OBS token management for the dashboard (create shown once, list, revoke, rotation grace)
  queue.py       the audio queue: atomic claim (SKIP LOCKED) and replay, with their realtime events
  service.py     the use-cases behind app.routers.obs
  static/        the OBS browser-source audio player (vanilla JS, no third-party code)
"""
