# Operator failure alerts

Failures that need an operator are recorded in `public.system_alerts` (migration 0005; its columns are used as they
are). They can be sent as one Discord message each to an operator channel (owner decision 2026-11-25). Code:
`backend/app/alerts.py`. The loop runs in the worker (`python -m app.worker`), next to the job slots and the realtime
publisher.

## What is alerted

| Kind | Source | Written when | Code (`message`) | Ids (`context`) |
|---|---|---|---|---|
| `job_failed` | `worker` | a background job finishes `failed`: its attempts ran out, or it hit a permanent error | the job's reason code (`provider_auth`, `max_attempts`, ...) | `job_id`, `job_kind`, `dedupe_key` (the job's subject id) |
| `payment_on_hold` | `payments` | a creator's `streamer_payment_health` row goes on hold, or its hold reason changes; a hold that clears and comes back is alerted again | `invalid_key` or `missing_key` | `streamer_id` |

- **Severity:** every alert is `error`.
- **What an alert holds:** only the kind, the source, a short code, database ids and timestamps. It never holds a donor name, a message, a token or a key.
- **Recording never fails the work:** if a job's alert cannot be written, the failure is logged and the job stays `failed` as before.
- **Payment holds:** the loop reads the hold from the health row that checkout writes. The payments code is not changed while the PAY-15 soak runs. `network_error` is transient (checkout answers 502), so it is never alerted. The loop remembers which creators were on hold at its last scan. After a worker restart, a hold that already has an alert with the same code is not alerted again.
- **Backups:** no backend code path reports a backup failure, so backups have no alert here. Backup and off-site failures stay with the host's systemd `OnFailure` units from the S0-6 monitoring work. Those units are installed on the server and are not in this repository.

## Delivery

```
HYPERCHAT_ALERT_DISCORD_CHANNEL_ID=        # unset (default): alerts are recorded only, nothing is sent
HYPERCHAT_DISCORD_BOT_TOKEN=               # the bots' token posts the message (the bots themselves may stay off)
HYPERCHAT_ALERT_POLL_SECONDS=30            # loop interval and retry backoff base (5..3600)
```

- **When it sends:** only when both the channel and the bot token are set. Delivery does not need `HYPERCHAT_BOTS_ENABLED`.
- **Outside production:** the channel must also be on `HYPERCHAT_BOT_RECIPIENT_ALLOWLIST` (bots D13). Otherwise the worker logs `not_allowlisted` and only records alerts.
- **The message:** one per alert, holding the environment, kind, source, severity, code, ids, time and alert id. Mentions are never parsed (`allowed_mentions: {parse: []}`).
- **Claiming:** each cycle claims the oldest undelivered alerts with `FOR UPDATE SKIP LOCKED`. Two workers never send the same alert.
- **Outcomes:**

| Outcome | Recorded on the alert |
|---|---|
| sent | `notified_at` set |
| 429, 5xx, timeout or network error | the attempt is counted (`context.delivery_attempts`, `delivery_attempted_at`) and `delivery_error` is recorded. The next try waits `interval × 2^(attempts−1)`. |
| 401, 403, 404 or another 4xx | given up at once, with `delivery_error` |
| 5 attempts used | never sent again. It stays undelivered and listed. |

Delivery is at-least-once: if a message was sent but its `notified_at` write was lost, it is sent again.

## Reading alerts

`GET /api/v1/admin/alerts?kind=job_failed|payment_on_hold&delivered=true|false&limit&offset` is admins only and
`no-store`. It lists alerts newest first, with their delivery state. Alerts that are not delivered, including ones
that were given up, show `notified_at: null` and their `delivery_error`.

## Follow-up (after the PAY-15 soak)

- **A durable delivery job:** a job kind such as `alert_notify` needs a migration that widens `jobs_kind_check` (0037 or later). The PAY-15 verifier accepts only the heads 0034 and 0035, so that migration and the verifier's pin widening wait until after the soak.
- **A direct hold hook:** a hook in the payments health path could replace the scan of the health rows.
