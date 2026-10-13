"""Creator soundboard and the platform hypersound library (Phase 6).

  rules.py       limits, content types, key ownership, display names (pure)
  audio.py       MP3 validation and server-side duration (mutagen + an independent frame count)
  library.py     public hypersound library listed from R2 (hypersounds/ + hypersound-thumbs/)
  repository.py  SQL: the legacy soundboard RPCs as transactions under a per-creator lock
  service.py     use-cases: board, edits, restore, presigned uploads, create/swap/replace/delete
  models.py      request/response models
  terms.py       Custom Soundboard terms version and the eligible-AND-accepted rule (D3)
"""
