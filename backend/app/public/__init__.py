"""Public read API (Phase 4): no login, no cookies, display-safe data only.

Rules (decisions D1-D10, see docs/public-api.md):
  * only `active` creators are public; pending/rejected/unknown/malformed slugs all get the same 404;
  * every response is an explicit model with a fixed field set (no emails, user/creator ids, tokens,
    keys, order/provider ids, messages or individual donation amounts);
  * snake_case fields; Cache-Control on every success; E7/E8 cached in-process;
  * per-IP rate limiting happens in Nginx (deploy/nginx/snippets), not in the database.
"""
