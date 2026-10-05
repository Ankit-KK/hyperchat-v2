# Deployment (VPS)

One Ubuntu VPS runs Nginx, the FastAPI API (systemd, Uvicorn on 127.0.0.1:8000), PostgreSQL and,
later, the background worker. The frontend is deployed separately (Vercel).

| Environment | API host | Database | Status |
|---|---|---|---|
| v2 development | `api.quicktestspace.site` | `hyperchat_dev` on cluster `15/restoretest` (port 5433) | running |
| production | `api.hyperchat.space` | `hyperchat` on cluster `17/hyperchat` (port 5432) | schema only, no traffic |

`hyperchat_restore_test` (also on `15/restoretest`) is the read-only restore of the legacy
production dump. It is a migration source only and never an application database.

## Contents
```
systemd/hyperchat-api.service   API unit (runs as user hyperchat, hardened, never root)
systemd/hyperchat-cleanup.{service,timer}  daily cleanup of expired auth/security rows (03:15 UTC)
nginx/00-catchall.conf          drops unknown hostnames / bare-IP requests
nginx/api.quicktestspace.site.conf   v2 development API vhost (TLS via Let's Encrypt)
nginx/snippets/                 per-IP rate limit for the public read API (install to /etc/nginx/snippets/)
postgres/setup_cluster.sh       create/check a cluster, roles and peer-auth mapping (idempotent)
postgres/roles.sql              hyperchat_owner / migrator / app / worker / readonly
postgres/pg_hba.conf, pg_ident.conf  Unix-socket peer auth only; PostgreSQL is never exposed
backup/                         nightly pg_dump of production (02:30 UTC, 14-day retention)
api.env.example                 template for /etc/hyperchat/api.env (no secrets)
```

## Release layout
```
/opt/hyperchat/releases/<commit>/{backend/, database/, venv/, backend/RELEASE}
/opt/hyperchat/current -> releases/<commit>
/etc/hyperchat/api.env                      (root:hyperchat 0640, never in Git)
/etc/systemd/system/hyperchat-api.service   (from deploy/systemd)
```

## Deploy a commit
1. `git archive <commit> backend database | ssh root@VPS "mkdir -p /opt/hyperchat/releases/<commit> && tar -x -C /opt/hyperchat/releases/<commit>"`
2. `python3.14 -m venv .../venv && .../venv/bin/pip install -r .../backend/requirements.txt`
3. `echo <commit> > .../backend/RELEASE`; `chown -R root:hyperchat .../<commit>`; `chmod -R g+rX,g-w,o-rwx .../<commit>`
4. Migrations (as root, peer-mapped to hyperchat_migrator):
   `cd .../database && HYPERCHAT_MIGRATION_DSN="postgresql+psycopg://hyperchat_migrator@/<db>?host=/var/run/postgresql&port=<port>" ../venv/bin/alembic upgrade head`
5. `ln -sfn /opt/hyperchat/releases/<commit> /opt/hyperchat/current && systemctl restart hyperchat-api`
6. `curl -fsS http://127.0.0.1:8000/health`, then the end-to-end check in `tests/e2e/`
   (on the dev API: `HYPERCHAT_SMOKE_SAMESITE=none`, matching its cross-site cookie setting).

First admin on a fresh environment (prompts for the password on the server, refuses once an admin
can sign in): `cd .../current/backend && ../venv/bin/python -m app.cli --env-file /etc/hyperchat/api.env
bootstrap-admin --email <admin email>`.

Cleanup timer: `install -m 644 deploy/systemd/hyperchat-cleanup.{service,timer} /etc/systemd/system/`,
`systemctl daemon-reload && systemctl enable --now hyperchat-cleanup.timer`.

Rollback: point `current` back to the previous release (and restore the previous
`/etc/hyperchat/api.env` backup if it changed), then restart.

## Rules
- Firewall (UFW): SSH (rate-limited), 80 and 443 only. PostgreSQL listens on localhost / Unix socket only.
- The API runs as `hyperchat` and connects as `hyperchat_app` (DML only); migrations run as
  `hyperchat_migrator` (`SET ROLE hyperchat_owner`). Nothing uses the `postgres` superuser at runtime.
- Secrets are generated on the server into `/etc/hyperchat/api.env`; they never go into Git or logs.
