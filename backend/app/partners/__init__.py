"""Partner API (D4: ClipWave). Server-to-server only, authenticated by a per-partner x-api-key whose SHA-256 is
stored in partner_api_keys (app.security.shared_secret); the partner is always the key's partner_slug.

  validation.py  the legacy partner-signup / partner-check-streamer field rules and slug normalization (pure)
  repository.py  SQL: partner_signups, the creator-exists check, partner key management (admin)
"""
