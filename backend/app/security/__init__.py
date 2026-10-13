"""Framework-independent security building blocks for HyperChat v2.

Browser authentication (cookie sessions + CSRF) lives in app.auth. This package holds the
mechanisms for everything that is not a browser session:
  crypto.py          hashing / HMAC / constant-time helpers
  audit.py           structured security events (log-safe fields only)
  cors.py            CORS middleware for the v2 frontend origins
  nonce_store.py     single-use nonce storage (replay protection)
  upload_grant.py    short-lived, single-use upload grants (donor media -> R2)
  obs_token.py       OBS overlay tokens (stored as SHA-256) and Pusher channel scopes
  shared_secret.py   partner API keys (x-api-key, stored as SHA-256)
Database-backed stores for these protocols are added with the features that use them.
"""
