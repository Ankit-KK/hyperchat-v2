# Deployment (VPS)

One Ubuntu VPS runs Nginx, the FastAPI API (systemd, Uvicorn on 127.0.0.1:8000), PostgreSQL and,
later, the background worker. The frontend is deployed separately (Vercel).

| Environment | API host | Database | Status |
|---|---|---|---|
| v2 development | `api.quicktestspace.site` | `hyperchat_dev` on cluster `15/restoretest` (port 5433) | running; the only environment for development and testing |
| production | `api.hyperchat.space` | `hyperchat` on cluster `17/hyperchat` (port 5432) | reserved: deployed and idle until the final cutover phase |

The development API trusts only development frontend origins (`HYPERCHAT_FRONTEND_URL=https://app.quicktestspace.site`,
`HYPERCHAT_ALLOWED_ORIGINS=http://localhost:5173`); it never trusts the production site.

`hyperchat_restore_test` (also on `15/restoretest`) is the read-only restore of the legacy
production dump. It is a migration source only and never an application database.

## Contents
```
systemd/hyperchat-api.service   API unit (runs as user hyperchat, hardened, never root)
systemd/hyperchat-cleanup.{service,timer}  daily cleanup of expired auth/security rows (03:15 UTC)
systemd/hyperchat-worker.service  DEV background worker (Phase 8: donation audio, voice transcription)
systemd/hyperchat-prod-api.service, hyperchat-prod-cleanup.{service,timer}  PRODUCTION API + cleanup (03:45 UTC)
systemd/hyperchat-prod-worker.service  PRODUCTION background worker (jobs, realtime outbox, notifications; not installed yet)
nginx/00-catchall.conf          drops unknown hostnames / bare-IP requests
nginx/api.quicktestspace.site.conf   v2 development API vhost (TLS via Let's Encrypt)
nginx/api.hyperchat.space.acme.conf  PRODUCTION vhost, bootstrap phase (port 80 only, for the first certificate)
nginx/api.hyperchat.space.conf  PRODUCTION API vhost (port 80 + 443 -> 127.0.0.1:8001)
nginx/snippets/                 per-IP rate limit for the public read API (install to /etc/nginx/snippets/):
                                hyperchat-public-limit*.conf (DEV zone), hyperchat-prod-public-limit*.conf (production zone)
postgres/setup_cluster.sh       create/check a cluster, roles and peer-auth mapping (idempotent)
postgres/roles.sql              hyperchat_owner / migrator / app / worker / readonly / lifecycle (KYC PR4:
                                creator status transitions only; re-run roles.sql and the pg_ident map before
                                migration 0023)
                                / auth (migration 0024: authentication flows; the only role that writes admin
                                credentials or sessions; HYPERCHAT_DB_AUTH_USER; docs/admin-auth-boundary.md)
postgres/pg_hba.conf, pg_ident.conf  Unix-socket peer auth only; PostgreSQL is never exposed. pg_hba.conf lists
                                EVERY login role (a missing one falls through to the generic peer rule and
                                cannot connect); sync_pg_hba.sh places the HyperChat rule above every local
                                rule that could match its roles (PostgreSQL uses the first match), or fails closed
check_isolation.sh              read-only server audit of one environment (dev|prod): file ownership, every
                                symlink hop and ancestor directory of the code, mount points, units, service
                                account, pg_hba coverage AND rule order, pg_ident; needs namei (util-linux)
postgres/pg_ident.production.conf    ident map of the PRODUCTION cluster only (hyperchat-prod; no DEV user)
backup/                         nightly pg_dump of production (02:30 UTC, 14-day retention)
systemd/hyperchat[-prod]-stepup.{socket,service}  step-up (passkey) verifier, S1: its own OS user, no network,
                                a socket only the API user reaches (docs/admin-stepup.md; not installed yet)
stepup.env[.production].example template for /etc/hyperchat[-prod]-stepup/stepup.env (verifier settings only, no secrets)
api.env.example                 template for /etc/hyperchat/api.env (DEV; no secrets)
api.env.production.example      template for /etc/hyperchat-prod/api.env (PRODUCTION; no secrets)
```

## Release layout
```
/opt/hyperchat/releases/<commit>/{backend/, database/, venv/, backend/RELEASE}
/opt/hyperchat/current -> releases/<commit>
/etc/hyperchat/api.env                      (root:hyperchat 0640, never in Git)
/etc/systemd/system/hyperchat-api.service   (from deploy/systemd)
/etc/nginx/sites-available/api.quicktestspace.site   (from deploy/nginx)
/etc/nginx/snippets/hyperchat-public-limit{,-zone}.conf   (from deploy/nginx/snippets)
```

## Deploy a commit
1. `git archive <commit> backend database | ssh root@VPS "mkdir -p /opt/hyperchat/releases/<commit> && tar -x -C /opt/hyperchat/releases/<commit>"`
2. `python3.14 -m venv .../venv && .../venv/bin/pip install -r .../backend/requirements.txt`
3. `echo <commit> > .../backend/RELEASE`; `chown -R root:hyperchat .../<commit>`; `chmod -R g+rX,g-w,o-rwx .../<commit>`
4. Migrations (as root, peer-mapped to hyperchat_migrator):
   `cd .../database && HYPERCHAT_MIGRATION_DSN="postgresql+psycopg://hyperchat_migrator@/<db>?host=/var/run/postgresql&port=<port>" ../venv/bin/alembic upgrade head`
   (Phase 5 brings `0009`: one nullable column, `moderation_actions.actor_user_id`; no table rewrite,
   its foreign key briefly locks `users` against writes (bounded by `lock_timeout` = 10 s).
   Apply it before step 5: the Phase 5 code writes the column, the previous release ignores it, so a
   code rollback needs no schema downgrade.)
5. `ln -sfn /opt/hyperchat/releases/<commit> /opt/hyperchat/current && systemctl restart hyperchat-api`
   From Phase 8 on, also the worker (same release, environment file and user):
   first time `install -o root -g root -m 644 deploy/systemd/hyperchat-worker.service /etc/systemd/system/`,
   `systemctl daemon-reload && systemctl enable --now hyperchat-worker`; afterwards
   `systemctl restart hyperchat-worker` with every release switch. (Phase 8 brings `0011`: two new tables,
   `jobs` and `donation_transcripts`, no change to existing ones; the previous release ignores them.
   Phase 9 brings `0012`: one new table, `realtime_outbox`; the previous release ignores it. Realtime stays
   off until `HYPERCHAT_REALTIME_ENABLED=true` and a Pusher app is configured, see `docs/realtime.md`.
   Phase 10 brings `0013`: two widened CHECK constraints on `realtime_outbox`; the previous release ignores the
   new values. The DEV nginx site changed (query-free logging of `/api/v1/obs/` and `/api/v1/realtime/`): repeat step 6.
   Phase 11 brings `0014`: a widened `jobs.kind` and `moderation_actions.action_source`, the new
   `moderator_notifications` table and two columns on each callback table; the previous release ignores them.
   Bots stay off until `HYPERCHAT_BOTS_ENABLED=true` with dedicated test bots, see `docs/bots.md`.
   `0015` drops one UNIQUE index on `moderation_actions` (no row changes); the previous release works without it,
   and its downgrade deliberately does not rebuild it.
   `0016` adds three nullable columns to `banned_donors` (no table rewrite; its foreign key briefly locks `users`
   against writes, bounded by `lock_timeout`); the previous release ignores them.
   `0018` (D6) moves `secondary` creators to `primary` and narrows the two Razorpay account CHECK constraints; it
   stops without changes if any donation was created on `secondary`. The previous release keeps working (it only
   ever writes `primary`). From this release on, production refuses to start with `rzp_test_` Razorpay keys and
   DEV with `rzp_live_` keys.)
6. Nginx (required from Phase 4 on: the public read API's per-IP rate limit; repeat whenever
   `deploy/nginx/` changed). As root, with the files of the same `<commit>`:
   - back up what is installed: `/etc/nginx/sites-available/api.quicktestspace.site` and any existing
     `/etc/nginx/snippets/hyperchat-public-limit*.conf` (e.g. to `/root/...bak-<commit>`);
   - install the snippets and the site file, owned by root, mode 0644:
     `install -o root -g root -m 644 deploy/nginx/snippets/hyperchat-public-limit-zone.conf deploy/nginx/snippets/hyperchat-public-limit.conf /etc/nginx/snippets/`
     `install -o root -g root -m 644 deploy/nginx/api.quicktestspace.site.conf /etc/nginx/sites-available/api.quicktestspace.site`;
   - `nginx -t` must pass **before** anything is reloaded; then `systemctl reload nginx`
     (a reload, not a restart: no dropped connections);
   - if `nginx -t` fails: restore the backups (remove snippets that did not exist before), confirm
     with `nginx -t` that the previous configuration is back, and do **not** reload.
7. `curl -fsS http://127.0.0.1:8000/health`, then the end-to-end checks in `tests/e2e/`
   (on the dev API: `HYPERCHAT_SMOKE_SAMESITE=none`, matching its cross-site cookie setting;
   `nginx_ratelimit_check.sh` verifies the rate-limit snippets).

First admin on a fresh environment (prompts for the password on the server, refuses once an admin
can sign in): `cd .../current/backend && ../venv/bin/python -m app.cli --env-file /etc/hyperchat/api.env
bootstrap-admin --email <admin email>`.

Cleanup timer: `install -m 644 deploy/systemd/hyperchat-cleanup.{service,timer} /etc/systemd/system/`,
`systemctl daemon-reload && systemctl enable --now hyperchat-cleanup.timer`.

Rollback: point `current` back to the previous release (and restore the previous
`/etc/hyperchat/api.env` backup if it changed), then restart `hyperchat-api` (and, from Phase 8 on,
`hyperchat-worker`; a release before Phase 8 has no `app.worker`: `systemctl stop hyperchat-worker` instead).

## Production API (`api.hyperchat.space`)
Reserved for the final cutover phase: V2 is developed and tested against DEV only. Production runs
next to DEV on the same VPS, fully separated:

| | DEV | PRODUCTION |
|---|---|---|
| Host name | `api.quicktestspace.site` | `api.hyperchat.space` |
| FastAPI bind | `127.0.0.1:8000` | `127.0.0.1:8001` |
| OS user | `hyperchat` | `hyperchat-prod` |
| Releases / current | `/opt/hyperchat/releases/<commit>`, `/opt/hyperchat/current` | `/opt/hyperchat-prod/releases/<commit>`, `/opt/hyperchat-prod/current` |
| Environment file | `/etc/hyperchat/api.env` | `/etc/hyperchat-prod/api.env` (root:hyperchat-prod 0640) |
| systemd | `hyperchat-api`, `hyperchat-worker` (Phase 8), `hyperchat-cleanup.timer` | `hyperchat-prod-api`, `hyperchat-prod-worker` (not installed yet, below), `hyperchat-prod-cleanup.timer` |
| Database | `hyperchat_dev`, cluster `15/restoretest` :5433 | `hyperchat`, cluster `17/hyperchat` :5432 |
| Ident map | `pg_ident.conf` | `pg_ident.production.conf` (the DEV user has no mapping) |
| Rate-limit zone | `hyperchat_public` | `hyperchat_public_prod` |

Steps marked **[YOU]** are your action; **[APPROVAL]** need explicit approval before they run. Every
step runs as root with the files of the same reviewed `<commit>`; stop on the first failure. DEV files
are never edited: record their fingerprints first (`sha256sum /etc/hyperchat/api.env
/etc/nginx/sites-available/api.quicktestspace.site /etc/nginx/snippets/hyperchat-public-limit*.conf
/etc/systemd/system/hyperchat-*.service /etc/postgresql/15/restoretest/*.conf`) and compare at the end.

1. **DNS [YOU]** — `api.hyperchat.space A 129.121.132.159` (DNS only, no proxy; no AAAA: the VPS has
   no global IPv6). Check: `getent hosts api.hyperchat.space`.
2. **User and directories [APPROVAL]** —
   `useradd --system --home-dir /opt/hyperchat-prod --no-create-home --shell /usr/sbin/nologin hyperchat-prod`;
   `install -d -o root -g hyperchat-prod -m 750 /opt/hyperchat-prod /opt/hyperchat-prod/releases`.
3. **Nginx, port 80 only [APPROVAL]** —
   `install -o root -g root -m 644 deploy/nginx/api.hyperchat.space.acme.conf /etc/nginx/sites-available/api.hyperchat.space`;
   `ln -s /etc/nginx/sites-available/api.hyperchat.space /etc/nginx/sites-enabled/api.hyperchat.space`;
   `nginx -t` must pass, then `systemctl reload nginx`. Check:
   `curl -sI http://api.hyperchat.space/` → 301 to https.
4. **TLS [APPROVAL]** — `certbot certonly --webroot -w /var/www/acme -d api.hyperchat.space`
   (one certificate, this single name; renewed by `certbot.timer`). Check that
   `/etc/letsencrypt/live/api.hyperchat.space/{fullchain,privkey}.pem` exist and that the renewal
   reloads nginx (e.g. a `deploy-hook = systemctl reload nginx` in
   `/etc/letsencrypt/renewal/api.hyperchat.space.conf`).
5. **Production environment [APPROVAL]** —
   `install -d -o root -g hyperchat-prod -m 750 /etc/hyperchat-prod`;
   `install -o root -g hyperchat-prod -m 640 deploy/api.env.production.example /etc/hyperchat-prod/api.env`;
   generate the auth secret on the server without printing it:
   `printf 'HYPERCHAT_AUTH_SECRET=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" >> /etc/hyperchat-prod/api.env`.
   Check: it differs from DEV
   (`[ "$(grep -h ^HYPERCHAT_AUTH_SECRET= /etc/hyperchat/api.env)" != "$(grep -h ^HYPERCHAT_AUTH_SECRET= /etc/hyperchat-prod/api.env)" ]`),
   no `localhost` origin, `SAMESITE=lax`, `APP_ENV=production`, `DB_NAME=hyperchat`, `DB_PORT=5432`.
6. **PostgreSQL isolation, production cluster only [APPROVAL]** — replace the `hyperchat` map lines of
   `17/hyperchat` with `pg_ident.production.conf` (the DEV cluster is not touched):
   ```
   P=/etc/postgresql/17/hyperchat/pg_ident.conf; cp -p $P /root/pg_ident.17.bak-<commit>
   { grep -v '^hyperchat[[:space:]]' $P; grep -v '^#' deploy/postgres/pg_ident.production.conf | grep -v '^[[:space:]]*$'; } > $P.new
   install -o postgres -g postgres -m 640 $P.new $P && rm $P.new && pg_ctlcluster 17 hyperchat reload
   ```
   Verify: `sudo -u hyperchat psql -h /var/run/postgresql -p 5432 -U hyperchat_app -d hyperchat -c 'select 1'`
   **fails** (DEV user denied); the same as `hyperchat-prod` succeeds; the DEV API still works
   (`curl -fsS http://127.0.0.1:8000/health` → database ok); `/etc/postgresql/15/restoretest/pg_ident.conf`
   is unchanged. `setup_cluster.sh` does not re-add the DEV lines later (it only appends when no
   `hyperchat` map line exists); on a rebuilt production cluster repeat this step.
7. **Backup** — `systemctl start hyperchat-pgbackup.service`; record the new
   `/var/backups/hyperchat/hyperchat_<UTC>.dump`, `sha256sum -c` its `.sha256`, and `pg_restore -l` it.
8. **Release build** — as in "Deploy a commit" steps 1–3, under `/opt/hyperchat-prod/releases/<commit>`,
   owned `root:hyperchat-prod`; import check as `hyperchat-prod`
   (`sudo -u hyperchat-prod .../venv/bin/python -c "import app.main"`).
9. **Migration [APPROVAL]** — `alembic current` must be `0008`, then
   `cd /opt/hyperchat-prod/releases/<commit>/database && HYPERCHAT_MIGRATION_DSN="postgresql+psycopg://hyperchat_migrator@/hyperchat?host=/var/run/postgresql&port=5432" ../venv/bin/alembic upgrade head`
   → `0009`. Verify `moderation_actions.actor_user_id` (uuid, nullable, no default, FK `users` ON DELETE
   SET NULL, validated) and that every table count is unchanged.
10. **systemd** — `install -o root -g root -m 644 deploy/systemd/hyperchat-prod-*.{service,timer} /etc/systemd/system/`
    (the glob covers the API service, the cleanup service and its timer, and also copies the worker and verifier
    unit files, which this step does not enable: they have their own approved steps);
    `ln -sfn /opt/hyperchat-prod/releases/<commit> /opt/hyperchat-prod/current`;
    `systemctl daemon-reload && systemctl enable --now hyperchat-prod-api hyperchat-prod-cleanup.timer`.
    Check: `curl -fsS http://127.0.0.1:8001/health` → `"environment":"production"`, the PROD label,
    release `<commit>`, database ok; `NRestarts` 0.
11. **Nginx, HTTPS [APPROVAL]** —
    `install -o root -g root -m 644 deploy/nginx/snippets/hyperchat-prod-public-limit-zone.conf deploy/nginx/snippets/hyperchat-prod-public-limit.conf /etc/nginx/snippets/`;
    `install -o root -g root -m 644 deploy/nginx/api.hyperchat.space.conf /etc/nginx/sites-available/api.hyperchat.space`;
    `nginx -t`, then `systemctl reload nginx` (if it fails: restore the port-80 file, remove the two
    production snippets, `nginx -t`, no reload).
12. **Health** — `curl -fsS https://api.hyperchat.space/health` → only `{"status":"ok","database":{"status":"ok"}}` (release,
    environment and label are shown to 127.0.0.1 callers only, security audit F5); `curl -sI http://api.hyperchat.space/` → 301;
    bare IP still 444 / handshake rejected; listeners: only 22/80/443 public, 5432/5433/8000/8001 on
    127.0.0.1; DEV fingerprints unchanged; journals and nginx error log clean.
13. **Smoke tests** — read-only first (Phase 4 public endpoints, unauthenticated 401/403/CSRF/CORS
    checks). The first admin is created by you **[YOU]**:
    `cd /opt/hyperchat-prod/current/backend && ../venv/bin/python -m app.cli --env-file /etc/hyperchat-prod/api.env bootstrap-admin --email <admin email>`.
    Authenticated checks against production need an account you choose **[APPROVAL]**.
    Payments (after the live Razorpay keys are set **[APPROVAL]**): `cd /opt/hyperchat-prod/current/backend &&
    ../venv/bin/python -m app.cli --env-file /etc/hyperchat-prod/api.env payment-health` must print `mode=live
    status=ok` and exit 0 before any donor checkout (D7: one read-only probe, no order). On DEV the same command
    must print `mode=test`.

Production rollback (no step is automatic):
- application: `ln -sfn /opt/hyperchat-prod/releases/<previous> /opt/hyperchat-prod/current && systemctl restart hyperchat-prod-api`;
  first release: `systemctl disable --now hyperchat-prod-api hyperchat-prod-cleanup.timer`;
- Nginx: `rm /etc/nginx/sites-enabled/api.hyperchat.space` (and the two production snippets), `nginx -t`, reload;
- migration: 0009 stays (the previous release ignores the column); `alembic downgrade 0008` only by
  explicit decision; restoring the backup over production only with explicit approval;
- PostgreSQL map: restore `/root/pg_ident.17.bak-<commit>`, `pg_ctlcluster 17 hyperchat reload`;
- environment: remove the production units, `/etc/hyperchat-prod`, `/opt/hyperchat-prod` and the user.

## Production worker (`hyperchat-prod-worker`, not installed; every step needs approval)
`deploy/systemd/hyperchat-prod-worker.service` runs `python -m app.worker` for production: the job queue (donation
audio / TTS, voice transcription, moderator notifications) and the realtime outbox publisher (Pusher). It is the DEV
worker unit with only the user, paths and environment file changed (a test keeps the two identical otherwise).

| | |
|---|---|
| OS user / group | `hyperchat-prod` (the production API's user; never root, never `hyperchat`) |
| Release | `/opt/hyperchat-prod/current`: the **same** release as `hyperchat-prod-api`, switched together |
| Environment | `/etc/hyperchat-prod/api.env` (root:hyperchat-prod 0640): the API's file; no separate worker file |
| Database role | `HYPERCHAT_DB_USER=hyperchat_app` (DML only) over the Unix socket, peer-mapped by `pg_ident.production.conf` (`hyperchat-prod → hyperchat_app`). No password, no migrator, owner, step-up or DEV mapping. The DEV user has no mapping on this cluster. |
| Network | No listening port or socket. Outbound HTTPS only: R2, Pusher, Telegram / Discord, and the paid audio providers only when enabled. `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX` |
| Hardening | DEV's: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, kernel / cgroup / clock / hostname protection, `RestrictNamespaces`, `RestrictSUIDSGID`, `LockPersonality`, `SystemCallArchitectures=native`, empty `CapabilityBoundingSet` and `AmbientCapabilities`, `UMask=0077`, `MemoryMax=1G`; writes nothing on disk (R2 and PostgreSQL only) |
| Stop / restart | `KillSignal=SIGTERM`, `TimeoutStopSec=60`; `Restart=always` after 3 s, at most 10 starts per 5 minutes |

### What it depends on
- **Migrations:** the release's schema (jobs 0011, realtime outbox 0012/0013, bots 0014/0015). Production is recorded
  at 0009, so the worker comes **with or after** the production migration to the release's head, never before.
- **Credentials** (all production-only, set on the server, never copied from DEV; each enabled only with approval;
  `api.env.production.example`):
  - R2 bucket and custom media domain: donation audio needs them (without R2 the worker does not take
    donation-audio jobs at all).
  - Pusher app(s) + `HYPERCHAT_REALTIME_ENABLED=true`: OBS alerts and live dashboard updates.
  - `HYPERCHAT_TTS_MODERATION_HASH_KEY` + `HYPERCHAT_AUDIO_PROVIDERS_ENABLED=true` + MiniMax / ElevenLabs / Sarvam keys:
    spoken alerts, sound effects, transcription.
  - Telegram / Discord bots + `HYPERCHAT_BOTS_ENABLED=true`: moderator notifications.

  With everything off (the template's defaults) the worker starts, logs what is not configured, marks realtime
  events `skipped` and finishes notifications as `bots_disabled`; it sends nothing anywhere.

### If the worker is stopped while the API runs
Nothing is lost; work waits in PostgreSQL:
- **Donations and payments** complete as usual (the API and webhooks do that).
- **Realtime (OBS alerts, dashboard live updates)** is not published. Events stay in `realtime_outbox`. An event
  older than its maximum age when the worker returns (10 minutes; `audio-now-playing` 30 s) is marked `expired`, not
  sent: a stale alert is never shown late.
- **Donation audio / TTS** is not generated; the jobs wait in `jobs` and run when the worker returns.
- **Moderator notifications** wait likewise and are sent once when the worker returns.
- **Transcription** requests are queued and answered later.

### Install (later, approved; nothing here was run)
1. Production API deployed on the target release, migrations at its head, `check_isolation.sh prod` all `OK`.
2. Credentials set in `/etc/hyperchat-prod/api.env` as approved (root, mode 0640 kept); everything else stays off.
3. `install -o root -g root -m 644 deploy/systemd/hyperchat-prod-worker.service /etc/systemd/system/` (from the
   same reviewed `<commit>` as the release); `systemctl daemon-reload`.
4. Import check as the service user: `sudo -u hyperchat-prod /opt/hyperchat-prod/current/venv/bin/python -c
   "import app.worker"`.
5. `systemctl enable --now hyperchat-prod-worker`.

### Release switches (API and worker together)
Same sequence as DEV ("Deploy a commit"): build the release; migrate; `ln -sfn` the new release to `current`; then
`systemctl restart hyperchat-prod-api hyperchat-prod-worker`. The worker always runs the release `current` points
to, so after a restart both run the same code; never restart one without the other after a switch.
- **Stopping:** `systemctl stop hyperchat-prod-worker` sends SIGTERM. The worker stops claiming; running jobs get up
  to 50 s to finish, then are released (status `queued`, the interrupted attempt not counted). A claimed realtime
  batch is released at once. Everything stays in PostgreSQL.
- **A crash or kill:** jobs and outbox rows keep their lease (jobs 3 minutes, outbox 30 s) and are
  claimed again after it expires; that counts as an attempt.

### Verification and logs
- `systemctl status hyperchat-prod-worker` (active, `NRestarts` 0); `journalctl -u hyperchat-prod-worker -n 100`.
- The first lines are `worker configuration` and `realtime configuration`: release, database label (must be the
  `PROD` label), concurrency, which providers / Pusher groups / bots are configured (names and booleans only, never
  keys), and `pusher_invalid` problems. Logs carry job and event ids, kinds, attempts and reason codes; never
  payloads, donor data, tokens or secrets.
- In the database (read-only, as `hyperchat_readonly`): `SELECT kind, status, count(*) FROM public.jobs GROUP BY 1, 2`
  and `SELECT status, count(*) FROM public.realtime_outbox WHERE created_at > now() - interval '1 hour' GROUP BY 1`:
  nothing `running` / `publishing` for long, `failed` only with a reason.
- **Troubleshooting:**
  - `database_disabled` at start: the environment file or the peer mapping is wrong.
  - Restarts every 3 s: the journal names the cause (a settings refusal, e.g. a test Razorpay key in production).
  - Realtime `skipped` with `pusher_not_configured`: a creator's Pusher group has no app.
  - Jobs never claimed: their handler is not built (no R2 means no donation audio).

### Rollback
- **Code:** point `current` back and `systemctl restart hyperchat-prod-api hyperchat-prod-worker` (same as the API).
- **Stop the worker only:** `systemctl disable --now hyperchat-prod-worker`. Safe at any time (above); nothing is
  lost, realtime alerts stop.
- **First install:** `systemctl disable --now hyperchat-prod-worker && rm /etc/systemd/system/hyperchat-prod-worker.service
  && systemctl daemon-reload`.
- **An integration that misbehaves:** set its switch back to `false` in `api.env` and restart **both** units (the API
  reads the same switches).

### Isolation checks
- `check_isolation.sh prod` now also checks `hyperchat-prod-worker.service`: root-owned, not group/other-writable,
  `User=hyperchat-prod`.
- The DEV files are never edited. Record their fingerprints before and compare after, as in the production section
  above.

### Idempotency
- **Jobs:** a job is finished, retried or released only by the worker holding its lease (fenced on `locked_by`), so a
  restart never completes a job twice.
- **Moderator notifications:** recorded per donation and recipient (`ON CONFLICT DO NOTHING`), so a retry never
  re-sends one that was already sent.
- **Realtime:** delivery is at-least-once. A publish whose `published` write was lost is sent again with the same
  `event_id`, which the overlays deduplicate.

## Isolation audit and the pg_hba fix (S0)
`pg_hba.conf` in this repository listed only migrator / app / worker / readonly until S0, and `setup_cluster.sh`
inserted the HyperChat line only once, so a cluster set up earlier keeps the old line: `hyperchat_lifecycle`
(migration 0023) and `hyperchat_auth` (0024) then fall through to the generic `local all all peer` rule, which has
no ident map, and cannot connect (admin sign-in and creator status changes fail). **Not applied to any server
yet; each step below needs approval.**

1. **Audit (read-only, safe):** from a root-only checkout of this commit,
   `bash deploy/check_isolation.sh dev` (and `prod`). Every line must be `OK`; a `FAIL` names the file or rule.
2. **Fix pg_hba [APPROVAL]:** `bash deploy/postgres/sync_pg_hba.sh deploy/postgres/pg_hba.conf
   /etc/postgresql/<ver>/<cluster>/pg_hba.conf | diff /etc/postgresql/<ver>/<cluster>/pg_hba.conf -` shows the
   change only (a HyperChat rule below a rule that shadows it is moved above it; include directives,
   continuation lines or two HyperChat rules are refused). To apply it, re-run `deploy/postgres/setup_cluster.sh <ver> <cluster> <port>` (it backs up
   `pg_hba.conf` to `pg_hba.conf.bak-<timestamp>`, installs the synced file and reloads; existing databases and
   the ident map are untouched). Rollback: copy the backup back, `pg_ctlcluster <ver> <cluster> reload`.
3. **pg_ident:** `setup_cluster.sh` still appends the DEV map only once and production uses
   `pg_ident.production.conf` (step 6 of the production section); the audit checks that each API user maps to
   exactly `hyperchat_app`, `hyperchat_worker`, `hyperchat_lifecycle` and `hyperchat_auth`.
4. **Ownership:** a `FAIL` on the release tree or `api.env` is fixed with "Deploy a commit" step 3
   (`chown -R root:<service> …; chmod -R g+rX,g-w,o-rwx …`) and `chown root:<service> api.env; chmod 640`.
   The release tree must stay unwritable by the service users: S1 runs the step-up verifier from the same
   release under its own user.
5. Re-run the audit; it must print only `OK`.

## Step-up verifier (S1, not deployed)
Install order, prerequisites (roles, pg_hba, pg_ident, migrations 0020-0025 on DEV and 0010-0025 on production,
PostgreSQL 15 verification) and the operator tool are in [`docs/admin-stepup.md`](../docs/admin-stepup.md)
("Deployment sequence"). Every step needs approval; `check_isolation.sh` audits the verifier once installed.

## Rules
- Firewall (UFW): SSH (rate-limited), 80 and 443 only. PostgreSQL listens on localhost / Unix socket only.
- The API runs as `hyperchat` (DEV) / `hyperchat-prod` (production) and connects as `hyperchat_app`
  (DML only); migrations run as `hyperchat_migrator` (`SET ROLE hyperchat_owner`). Nothing uses the
  `postgres` superuser at runtime. The DEV user has no mapping on the production cluster.
- Secrets are generated on the server into `/etc/hyperchat/api.env` (DEV) or
  `/etc/hyperchat-prod/api.env` (production, never copied from DEV); they never go into Git or logs.
