# Moderation bots (Phase 11)

Telegram and Discord moderation: new-donation notifications to moderators, Telegram commands and buttons, and Discord buttons. They replace the legacy `notify-new-donations`, `telegram-webhook`, `discord-webhook` and setup functions.

Code: `backend/app/bots/`, `backend/app/routers/bots.py`; migration `0014`. Moderation goes through the **shared core** (`DashboardRepository.moderate` with a `BotActor`), the same state machine, transaction, audio job and realtime events as the dashboard. There is no HTTP hop.

```
payment completion tx ── job moderator_notify (pending: now; auto-approved: when its audio is due)
worker ── re-read donation ── recipients ── moderator_notifications row each (idempotent) ── Telegram / Discord
Telegram ─► POST /api/v1/webhooks/telegram  (secret token, update_id dedupe) ─ commands | button
Discord  ─► POST /api/v1/webhooks/discord   (Ed25519 + timestamp ≤ 5 min, interaction-id dedupe) ─ button
button "m:<16 chars>" ─ stored row (action, donation, creator, recipient) ─ moderator + permission ─ shared core
```

## Decisions (approved D1–D15)

| # | Decision |
|---|---|
| D1 | Scope: moderator notifications, Telegram moderation (commands and buttons), Discord moderation (buttons). |
| D2 | One moderation code path for the dashboard and both bots. |
| D3 | Telegram `X-Telegram-Bot-Api-Secret-Token` (constant time); Discord Ed25519 with a timestamp ≤ 5 minutes off; update and interaction IDs deduplicated through `request_nonces`; webhooks registered only with the CLI's `--expect-bot` / `--expect-application-id` check. |
| D4 | Buttons carry `m:<16-character CSPRNG id>`. The stored row decides the action, donation and creator. Rows expire after 24 h. DM buttons are bound to their recipient and channel buttons to their channel. Legacy callback formats are refused. |
| D5 | `moderator_notify` job enqueued in the completion transaction. Each recipient gets a delivery row, and `mod_notified` is set when the job finishes. |
| D6 | Paid, pending or approved donations within **24 h**. **No historical backlog: nothing ever scans `mod_notified = false`.** Only donations completed after deployment are enqueued. Failed payments, `!vaani` and rejected donations are never notified. |
| D7 | 429 (`retry_after`), 5xx and network errors are retried; only the deliveries that failed are re-sent. 400/401/403/404 fail that one recipient. When attempts run out, leftover deliveries are marked `failed` and the donation counts as notified, so there's no endless loop. |
| D8 | Legacy layout. Donor text is HTML-escaped (Telegram) and markdown-escaped (Discord). Discord `allowed_mentions` is limited to the moderators the bot pings. |
| D9 | Approve, reject, hide and ban, checked at click time. Roles: owner = all, viewer = none, otherwise the `can_*` flags. The reply reports the real result. No bot replay (legacy never offered one). |
| D10 | `/start`, `/pending`, `/recent`, `/stats`, `/status` and `/settings` cover **every** creator the user moderates (outside production: only allow-listed creators, see D13). **`/stats` shows the pending count and the user's own action count, never revenue.** |
| D11 | `moderation_actions` records `telegram` and `discord` (CHECK widened), plus the moderator row and platform user ID. The moderator's `last_action_at` / `total_actions` are updated in the same transaction. |
| D12 | Discord invite endpoint: deferred (the current frontend doesn't need it). |
| D13 | **Bots off by default.** Outside production, **only allow-listed recipient IDs** (`HYPERCHAT_BOT_RECIPIENT_ALLOWLIST`) receive anything; an empty list means nobody. **The bots also act only for allow-listed creators** (`HYPERCHAT_BOT_CREATOR_ALLOWLIST`, slugs; empty = none): notifications, every command and every button press. A tester is often a moderator of real DEV creators too, and without this `/pending` would hand them live buttons for those creators. DEV uses separate test bots. |
| D14 | The legacy `@hyperChat_modbot` webhook and the legacy Discord application are never changed. Re-pointing them is cutover work. |
| D15 | Migration `0014`: `jobs.kind` gains `moderator_notify`; `moderation_actions.action_source` gains `discord`; new `moderator_notifications` table; the callback tables get text short IDs, `recipient_id` and `moderator_id`. Cleanup rules: callback rows 7 days after expiry, notifications after 30 days. |

## The notification job

`moderator_notify` (payload `{donation_id}`, 5 attempts, backoff 10 s × 2ⁿ capped at 5 min). How each case is handled:

| Situation | Result |
|---|---|
| donation missing or already notified | `skipped` |
| bots disabled | `skipped (bots_disabled)`, `mod_notified = true` |
| outside production, creator not allow-listed | `skipped (creator_not_allowlisted)`, `mod_notified = true` |
| not paid, not pending/approved, or older than 24 h | `skipped`, `mod_notified = true` |
| no recipients | `skipped (no_recipients)`, `mod_notified = true` |

Recipients:
- **Telegram:** a DM to each active moderator with a numeric Telegram ID, if Telegram moderation is on.
- **Discord:** if Discord moderation is on, one post in `discord_moderation_channel_id` that mentions up to 5 moderators. Without a channel, a DM to each moderator with a Discord ID.

How a delivery ends:
- **Sent:** status `sent`, with the platform's message ID.
- **Non-allowlisted recipient:** `skipped (not_allowlisted)`.
- **400/401/403/404:** `failed`.
- **Transient error:** stays `pending`, and the job is retried.

## Webhooks

- **Telegram** (private chats only):
  - **Commands:** 20 per minute per user.
  - **Button presses:** 60 per minute per user. The answer comes back in the webhook response (`answerCallbackQuery`); the clicked message is edited best effort, as plain text, gaining the result and losing its buttons.
- **Discord:**
  - **PING** gets PONG.
  - **Button presses** get a type 7 reply (result line, buttons removed) or an ephemeral type 4 refusal. No mention is ever parsed.
- **Both:**
  - **503** while bots are disabled or not configured.
  - **401** for a bad secret or signature, logged as a security event (reason only).
  - **Logs:** update kind and reason codes only. User IDs appear only as short hashes; update bodies, donor text, tokens and secrets never do.

## Configuration

```
HYPERCHAT_BOTS_ENABLED=false                  # default
HYPERCHAT_TELEGRAM_BOT_TOKEN=                 # DEV: a separate test bot
HYPERCHAT_TELEGRAM_WEBHOOK_SECRET=            # random; sent by Telegram as X-Telegram-Bot-Api-Secret-Token
HYPERCHAT_DISCORD_BOT_TOKEN=                  # DEV: a separate test application
HYPERCHAT_DISCORD_PUBLIC_KEY=                 # the application's Ed25519 public key (hex)
HYPERCHAT_DISCORD_APPLICATION_ID=
HYPERCHAT_BOT_RECIPIENT_ALLOWLIST=            # outside production: the only ids that may receive messages
HYPERCHAT_BOT_CREATOR_ALLOWLIST=              # outside production: the only creator slugs the bots act for
```

Registering the webhooks (root, on the server, only after you have chosen to enable them):

```
python -m app.cli --env-file /etc/hyperchat/api.env bots-set-telegram-webhook \
    --url https://<api host>/api/v1/webhooks/telegram --expect-bot <test bot username>
python -m app.cli --env-file /etc/hyperchat/api.env bots-set-discord-endpoint \
    --url https://<api host>/api/v1/webhooks/discord --expect-application-id <test application id>
```

Both refuse if the configured token belongs to another bot or application, so the legacy production bot can never be re-pointed by mistake.

## Deferred

- editing moderator permissions and self-service linking
- Discord slash commands; Telegram groups
- updating every recipient's message after an action
- payout and admin notifications
- a dashboard setting for the Discord channel
- the Discord invite endpoint
