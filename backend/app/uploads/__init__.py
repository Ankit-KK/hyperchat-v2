"""Donor uploads (Phase 6): upload grants, voice/media validation and storage in R2.

  media.py    content-type, signature and size rules (voice WebM, images), effective limits
  webm.py     strict WebM/Opus reader that measures voice duration server-side
  service.py  grant issuance and the upload use-cases (rate limits, creator checks, grant burn, R2 write)
"""
