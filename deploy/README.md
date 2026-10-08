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
nginx/00-catchall.conf          drops unknown hostnames / bare-IP requests
nginx/api.quicktestspace.site.conf   v2 development API vhost (TLS via Let's Encrypt)
nginx/api.hyperchat.space.acme.conf  PRODUCTION vhost, bootstrap phase (port 80 only, for the first certificate)
nginx/api.hyperchat.space.conf  PRODUCTION API vhost (port 80 + 443 -> 127.0.0.1:8001)
nginx/snippets/                 per-IP rate limit for the public read API (install to /etc/nginx/snippets/):
                                hyperchat-public-limit*.conf (DEV zone), hyperchat-prod-public-limit*.conf (production zone)
postgres/setup_cluster.sh       create/check a cluster, roles and peer-auth mapping (idempotent)
postgres/roles.sql              hyperchat_owner / migrator / app / worker / readonly
postgres/pg_hba.conf, pg_ident.conf  Unix-socket peer auth only; PostgreSQL is never exposed
postgres/pg_ident.production.conf    ident map of the PRODUCTION cluster only (hyperchat-prod; no DEV user)
backup/                         nightly pg_dump of production (02:30 UTC, 14-day retention)
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
   Bots stay off until `HYPERCHAT_BOTS_ENABLED=true` with dedicated test bots, see `docs/bots.md`.)
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
| systemd | `hyperchat-api`, `hyperchat-worker` (Phase 8), `hyperchat-cleanup.timer` | `hyperchat-prod-api`, `hyperchat-prod-cleanup.timer` |
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
    (the glob covers the API service, the cleanup service and its timer);
    `ln -sfn /opt/hyperchat-prod/releases/<commit> /opt/hyperchat-prod/current`;
    `systemctl daemon-reload && systemctl enable --now hyperchat-prod-api hyperchat-prod-cleanup.timer`.
    Check: `curl -fsS http://127.0.0.1:8001/health` → `"environment":"production"`, the PROD label,
    release `<commit>`, database ok; `NRestarts` 0.
11. **Nginx, HTTPS [APPROVAL]** —
    `install -o root -g root -m 644 deploy/nginx/snippets/hyperchat-prod-public-limit-zone.conf deploy/nginx/snippets/hyperchat-prod-public-limit.conf /etc/nginx/snippets/`;
    `install -o root -g root -m 644 deploy/nginx/api.hyperchat.space.conf /etc/nginx/sites-available/api.hyperchat.space`;
    `nginx -t`, then `systemctl reload nginx` (if it fails: restore the port-80 file, remove the two
    production snippets, `nginx -t`, no reload).
12. **Health** — `curl -fsS https://api.hyperchat.space/health`; `curl -sI http://api.hyperchat.space/` → 301;
    bare IP still 444 / handshake rejected; listeners: only 22/80/443 public, 5432/5433/8000/8001 on
    127.0.0.1; DEV fingerprints unchanged; journals and nginx error log clean.
13. **Smoke tests** — read-only first (Phase 4 public endpoints, unauthenticated 401/403/CSRF/CORS
    checks). The first admin is created by you **[YOU]**:
    `cd /opt/hyperchat-prod/current/backend && ../venv/bin/python -m app.cli --env-file /etc/hyperchat-prod/api.env bootstrap-admin --email <admin email>`.
    Authenticated checks against production need an account you choose **[APPROVAL]**.

Production rollback (no step is automatic):
- application: `ln -sfn /opt/hyperchat-prod/releases/<previous> /opt/hyperchat-prod/current && systemctl restart hyperchat-prod-api`;
  first release: `systemctl disable --now hyperchat-prod-api hyperchat-prod-cleanup.timer`;
- Nginx: `rm /etc/nginx/sites-enabled/api.hyperchat.space` (and the two production snippets), `nginx -t`, reload;
- migration: 0009 stays (the previous release ignores the column); `alembic downgrade 0008` only by
  explicit decision; restoring the backup over production only with explicit approval;
- PostgreSQL map: restore `/root/pg_ident.17.bak-<commit>`, `pg_ctlcluster 17 hyperchat reload`;
- environment: remove the production units, `/etc/hyperchat-prod`, `/opt/hyperchat-prod` and the user.

## Rules
- Firewall (UFW): SSH (rate-limited), 80 and 443 only. PostgreSQL listens on localhost / Unix socket only.
- The API runs as `hyperchat` (DEV) / `hyperchat-prod` (production) and connects as `hyperchat_app`
  (DML only); migrations run as `hyperchat_migrator` (`SET ROLE hyperchat_owner`). Nothing uses the
  `postgres` superuser at runtime. The DEV user has no mapping on the production cluster.
- Secrets are generated on the server into `/etc/hyperchat/api.env` (DEV) or
  `/etc/hyperchat-prod/api.env` (production, never copied from DEV); they never go into Git or logs.
