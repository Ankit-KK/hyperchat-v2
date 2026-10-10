# OBS URLs at cutover (OBS-4)

Creators' OBS scenes contain URLs that legacy HyperChat issued. This page maps every legacy URL shape to V2 and lists
what must be true at cutover so those scenes keep working. It is a plan: nothing here has been run, and no creator has
been contacted.

**Evidence:** the legacy source (`Ankit-KK/hyperchat`: `src/App.tsx`, `OBSTokenManager.tsx`, `GoalManager.tsx`,
`MediaSourcePlayer.tsx`, `supabase/functions/get-current-audio`, `generate-obs-token`) and read-only counts from
`hyperchat_dev` (the 2026-10-04 dump), on 2026-10-10.

## 1. What legacy issued

| # | URL a creator has in OBS | Issued by | Token |
|---|---|---|---|
| L1 | `https://hyperchat.space/{slug}/obs-alerts?token=T` (browser source) | dashboard OBS token manager (`OBSTokenManager.tsx:380`, `:498`) | `?token=` |
| L2 | `https://<project>.supabase.co/functions/v1/get-current-audio?token=T` (Media Source) | the same page (`:641`) and the `/{slug}/media-audio-player` helper page (`MediaSourcePlayer.tsx:94`) | `?token=` |
| L3 | `https://hyperchat.space/{slug}/goal-overlay` (browser source) | goal manager (`GoalManager.tsx:192`) | none (public goal) |
| L4 | `https://hyperchat.space/{slug}/media-audio-player` | a page where a creator pastes a token to get L2; not itself an OBS source | typed in |
| L5 | `https://hyperchat.space/{ankit, chiaa}/audio-player` | an older browser-source player (3 routes incl. the `chiaa_gaming` alias) | none |

- **Optional query parameters** (all overlays): `dpr`, `ss` or `scale` (`useOverlayScale.ts:24`). A browser source may also
  carry `token` on the goal overlay; it is optional there.
- **Token format:** `obs_` + 64 hex characters (`generate-obs-token`). The ETL stores the SHA-256 of the token
  (`database/etl/mapping.py` `obs_token_hash`), the same hash V2 looks tokens up by, so a legacy token value works
  unchanged in V2.
- **Route slugs differ from creator slugs in a few cases** (legacy `src/App.tsx` against `hyperchat_dev`):

  | Legacy route slug | Creator slug | Meaning |
  |---|---|---|
  | `chiaa` | `chiaa_gaming` | the pages live under `/chiaa/...`; `/chiaa_gaming/...` redirects there |
  | `altrui`, `gigi_is_live` | not in the DEV dump | routes exist; the creators were added after 2026-10-04 or removed; recheck on the cutover dump |
  | – | `aka`, `ashu`, `latifa` | creators without OBS routes (`gaming_with_latifa` has its own) |

## 2. V2 equivalents

| Legacy | V2 | Status |
|---|---|---|
| L1 alerts overlay | the same URL, served by the **new frontend** on `hyperchat.space`; the page calls the API with the token (`x-obs-token`): `GET /api/v1/public/streamers/{slug}/overlay-config`, `GET /api/v1/realtime/config`, `POST /api/v1/realtime/pusher/auth` (channels in the token's scope; the dashboard channel is never allowed), events `audio-now-playing`, `settings-updated`, `leaderboard-updated` (`docs/realtime.md`, `docs/obs-audio.md`) | API **TESTED** with legacy-format tokens (`tests/realtime/test_realtime_db.py`, `?token=` and header); page not built (phase 13) |
| L2 Media Source | `https://api.hyperchat.space/api/v1/obs/audio/next?token=T`: the same atomic claim, a 302 to the audio file, 204 when the queue is empty | API **TESTED** with legacy-format tokens (`tests/obs/test_obs_db.py`); the legacy host is `*.supabase.co`, which V2 cannot serve (decision **D15**) |
| L3 goal overlay | the same URL on the new frontend; `GET …/{slug}/goal` + public `{slug}-goal` `goal-progress` | API TESTED; page not built |
| L4 helper page | the dashboard's OBS token page shows V2's `media_source_url` and `player_url` when a token is created | API TESTED; page not built |
| L5 older player | V2's `GET /obs/player#token=T` (served by the API) replaces it; the 3 routes may redirect to a page that explains this | needs a frontend route |

New V2 tokens come with V2 URLs (`player_url`, `media_source_url`; `docs/obs-audio.md`). Legacy tokens stay valid
until revoked or expired; on DEV 143 of 218 migrated tokens are active, for 126 creators; none has a custom channel
scope.

## 3. Cutover compatibility checklist

Before the switch (frontend, phase 13):
- [ ] The new frontend serves `/{slug}/obs-alerts`, `/{slug}/goal-overlay`, `/{slug}/media-audio-player` and
      `/{slug}/audio-player` for **every** creator, reads `?token=` and `dpr` / `ss` / `scale` exactly as legacy did, and
      sends the token to the API in `x-obs-token`.
- [ ] Route aliases: `/chiaa/*` serves creator `chiaa_gaming`; `/chiaa_gaming/*` keeps working.
- [ ] Frontend tests open each route shape with a migrated (legacy-format) token against DEV and receive the overlay
      config and a Pusher subscription.

At cutover (phase 14):
- [ ] Re-run the route / slug comparison against the cutover dump (read-only; commands below) and resolve every
      mismatch (`altrui`, `gigi_is_live` and any creator added since 2026-10-04).
- [ ] Load `obs_tokens` from the fresh dump (hashes only), then check: the active-token count matches legacy, and one
      known synthetic token validates through `GET /api/v1/obs/audio/next` (204 on an empty queue).
- [ ] `hyperchat.space` DNS points at the new frontend; `api.hyperchat.space` serves the API (already provisioned).
- [ ] L2 Media Source URLs, per **D15**.

After cutover (phase 15):
- [ ] Keep legacy tokens valid until creators have new URLs; revoke them only with notice.
- [ ] Supabase shutdown only after the D15 plan for L2 is complete.

**D15: legacy Media Source URLs on `*.supabase.co`.** V2 cannot answer on that host, so every L2 URL stops working
when Supabase is shut down. Options:
1. **Creators update the Media Source URL** before shutdown (needs creator communication; out of scope here).
2. **Bridge:** during the cutover window, replace the legacy `get-current-audio` Edge Function with a 302 to
   `https://api.hyperchat.space/api/v1/obs/audio/next?token=<same token>`. Old URLs keep working until Supabase is
   shut down, which still requires option 1 before that. Unverified: OBS Media Source must follow two redirects
   (Supabase → API → audio file); legacy already relied on one. Needs a live OBS test first.

Recommendation: option 2 for the switch, with option 1 communicated well before Supabase shutdown.

## Commands (read-only)

```
# legacy route slugs, per route type (legacy checkout)
grep -o 'path="/[^"/]*/obs-alerts"' src/App.tsx | sed -E 's#path="/([^/]+)/.*#\1#' | sort -u > route_slugs.txt
# creator slugs (any database loaded from the dump)
psql -Atc "SELECT streamer_slug FROM public.streamers ORDER BY 1" > creator_slugs.txt
comm -23 route_slugs.txt creator_slugs.txt   # routes without a creator
comm -13 route_slugs.txt creator_slugs.txt   # creators without a route
# token counts (never token values)
psql -Atc "SELECT count(*) FILTER (WHERE is_active AND (expires_at IS NULL OR expires_at > now())), count(*) FROM public.obs_tokens"
```
