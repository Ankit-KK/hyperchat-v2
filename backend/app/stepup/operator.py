"""Administrator passkey operator tool (S1, migration 0025). Run ONLY by the operator on the server, as root with the
migration role (it acts as hyperchat_owner); the API, its database roles and the verifier cannot do any of this.

    HYPERCHAT_MIGRATION_DSN=... python -m app.stepup.operator <command>

  list [--email E]            read-only: every administrator's passkeys (status, fingerprint, created, last used)
  readiness                   read-only: MFA enforcement state and which active administrators still lack two
                              active passkeys (enforcement cannot be activated until none do)
  approve --email E --fingerprint F --verified-by TEXT --operator NAME --confirm-database DB
                              pending -> active. Only after confirming, independently of the API (in person, a
                              call to a known number), that the administrator sees exactly fingerprint F on the
                              screen where they registered it; --verified-by records how.
  revoke --email E --fingerprint F --reason TEXT --operator NAME --confirm-database DB
                              pending/active -> revoked (a lost or compromised authenticator)
  recover --email E --reason TEXT --operator NAME --confirm-database DB
                              lost every authenticator: revokes all of the administrator's passkeys and ends all of
                              their sessions; they then register new passkeys, approved with `approve`
  google-requests [--email E] read-only: open Google link requests (an administrator's Google sign-in was refused;
                              the person was shown the request's code)
  link-google --email E --code C --verified-by TEXT --operator NAME --confirm-database DB
                              link that request's exact Google account to that administrator. Only after confirming,
                              independently of the API, that this administrator made the request and sees code C.
                              Administrators never link Google themselves (migration 0026).
  reject-google --email E --code C --reason TEXT --operator NAME --confirm-database DB
Every change is a single audited database function (public.stepup_*); exit codes: 0 done, 2 refused.
"""

import argparse
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row


def _connect() -> psycopg.Connection:
    dsn = os.environ["HYPERCHAT_MIGRATION_DSN"].replace("postgresql+psycopg://", "postgresql://", 1)
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    conn.execute("SET ROLE hyperchat_owner")
    return conn


def _refuse(message: str) -> int:
    print(f"Refused: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.stepup.operator", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list").add_argument("--email")
    sub.add_parser("readiness")
    sub.add_parser("google-requests").add_argument("--email")
    for name in ("approve", "revoke", "recover", "link-google", "reject-google"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--email", required=True)
        cmd.add_argument("--operator", required=True)
        cmd.add_argument("--confirm-database", required=True)
    for name in ("approve", "revoke"):
        sub.choices[name].add_argument("--fingerprint", required=True)
    sub.choices["approve"].add_argument("--verified-by", required=True,
                                        help="how the administrator's identity and fingerprint were confirmed")
    for name in ("revoke", "recover", "reject-google"):
        sub.choices[name].add_argument("--reason", required=True)
    for name in ("link-google", "reject-google"):
        sub.choices[name].add_argument("--code", required=True)
    sub.choices["link-google"].add_argument("--verified-by", required=True,
                                            help="how the administrator confirmed this request was theirs")
    args = parser.parse_args(argv)

    with _connect() as conn:
        database = conn.execute("SELECT current_database() AS d").fetchone()["d"]
        if args.command == "list":
            rows = conn.execute("""
                SELECT u.email::text AS email, c.fingerprint, c.status, c.created_at, c.approved_by, c.last_used_at,
                       c.revoked_by, c.revoke_reason
                FROM public.webauthn_credentials c JOIN public.users u ON u.id = c.user_id
                WHERE %(email)s::citext IS NULL OR u.email = %(email)s::citext
                ORDER BY u.email, c.created_at
            """, {"email": args.email}).fetchall()
            for r in rows:
                print(f"{r['email']:<40} {r['fingerprint']}  {r['status']:<8} created {r['created_at']:%Y-%m-%d %H:%M}"
                      f"  approved_by={r['approved_by'] or '-'}  last_used={r['last_used_at'] or '-'}")
            return 0
        if args.command == "google-requests":
            rows = conn.execute("""
                SELECT u.email::text AS admin, r.code, r.email AS google_email, r.created_at, r.expires_at
                FROM public.admin_identity_link_requests r JOIN public.users u ON u.id = r.user_id
                WHERE r.decided_at IS NULL AND r.expires_at > now()
                  AND (%(email)s::citext IS NULL OR u.email = %(email)s::citext)
                ORDER BY r.created_at
            """, {"email": args.email}).fetchall()
            for r in rows:
                print(f"{r['admin']:<40} {r['code']}  google={r['google_email'] or '-'}  "
                      f"requested {r['created_at']:%Y-%m-%d %H:%M}  expires {r['expires_at']:%Y-%m-%d %H:%M}")
            return 0
        if args.command == "readiness":
            state = conn.execute("SELECT state, changed_at, changed_by FROM public.stepup_enforcement").fetchone()
            rows = conn.execute("SELECT * FROM public.stepup_enforcement_readiness()").fetchall()
            missing = [r for r in rows if r["active_credentials"] < 2]
            print(json.dumps({"database": database, "enforcement": state["state"], "admins": len(rows),
                              "ready": bool(rows) and not missing,
                              "need_more_passkeys": [{"email": r["email"], "active": r["active_credentials"],
                                                      "pending": r["pending_credentials"]} for r in missing]},
                             default=str))
            return 0

        if args.confirm_database != database:
            return _refuse(f"--confirm-database does not name this database ({database})")
        try:
            if args.command == "approve":
                outcome = conn.execute("SELECT public.stepup_approve_credential(%s, %s, %s, %s) AS r",
                                       (args.email, args.fingerprint, args.operator, args.verified_by)).fetchone()["r"]
            elif args.command == "link-google":
                outcome = conn.execute("SELECT public.admin_link_google_identity(%s, %s, %s, %s) AS r",
                                       (args.email, args.code, args.operator, args.verified_by)).fetchone()["r"]
            elif args.command == "reject-google":
                outcome = conn.execute("SELECT public.admin_reject_google_link(%s, %s, %s, %s) AS r",
                                       (args.email, args.code, args.operator, args.reason)).fetchone()["r"]
            elif args.command == "revoke":
                outcome = conn.execute("SELECT public.stepup_revoke_credential(%s, %s, %s, %s) AS r",
                                       (args.email, args.fingerprint, args.operator, args.reason)).fetchone()["r"]
            else:
                revoked = conn.execute("SELECT public.stepup_recover_admin(%s, %s, %s) AS r",
                                       (args.email, args.operator, args.reason)).fetchone()["r"]
                print(f"Recovery: {revoked} passkey(s) revoked and every session ended for {args.email}. "
                      "Have them register new passkeys, then approve each after verifying its fingerprint.")
                return 0
        except psycopg.errors.Error as exc:
            return _refuse(exc.diag.message_primary or exc.sqlstate or "database error")
        if outcome != "ok":
            return _refuse(outcome)
        print(f"{args.command}: done for {args.email} ({(getattr(args, 'fingerprint', None) or args.code).upper()}).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
