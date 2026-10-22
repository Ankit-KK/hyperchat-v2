"""Administrator passkey and Google-link operator tool (S1/S2, migrations 0025/0026). Run ONLY by the operator on the
server, as root with the migration role (it acts as hyperchat_owner); the API, its database roles and the verifier
cannot do any of this.

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
  link-google --email E --expect-google-email G --verified-by TEXT --operator NAME --confirm-database DB
                              link the Google account the administrator PROVES they control, verified here, never
                              through the API (administrators never link Google themselves; nothing the API shows or
                              records counts as proof). Two independent checks, both required:
                                1. the administrator's identity: confirm it out of band yourself (in person, or a
                                   call to a number you already had), and ask them which Google address G they will
                                   use; --verified-by records how you confirmed;
                                2. control of that exact Google account: this tool starts Google's device sign-in
                                   for the OPERATOR's OAuth client (HYPERCHAT_OPERATOR_GOOGLE_CLIENT_ID / _SECRET,
                                   a "TVs and limited input devices" client, never the API's), shows a URL and a
                                   code, and the administrator completes the sign-in ON THEIR OWN DEVICE while you
                                   are in contact. The ID token comes straight from Google to this tool and is
                                   verified (signature, issuer, this client as audience, issued within 10 minutes,
                                   verified email equal to G); its subject is what gets linked.
                              Never read the code to anyone you have not verified; abort if the person did not
                              expect it.
  enforcement-draft --reason TEXT --operator NAME --confirm-database DB
                              EMERGENCY ROLLBACK (e.g. administrators locked out): MFA enforcement active -> draft.
                              Admin routes then work without passkey assurance again; passkeys, grants and the audit
                              history are untouched. Recorded as an enforcement_changed event with the operator and
                              reason. Already draft: changes and records nothing (exit 0). Activation is NOT a command
                              here: it stays a separately approved step.
Every change is a single audited database function; exit codes: 0 done, 2 refused.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row

from app.auth.google import GoogleAuthError, GoogleOAuth


def _connect() -> psycopg.Connection:
    dsn = os.environ["HYPERCHAT_MIGRATION_DSN"].replace("postgresql+psycopg://", "postgresql://", 1)
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    conn.execute("SET ROLE hyperchat_owner")
    return conn


def _refuse(message: str) -> int:
    print(f"Refused: {message}", file=sys.stderr)
    return 2


def operator_google() -> GoogleOAuth:
    """The operator's own Google OAuth client (device flow). Never the API's web client: a token minted for one is
    useless for the other (audience)."""
    client_id = os.environ.get("HYPERCHAT_OPERATOR_GOOGLE_CLIENT_ID", "").strip()
    secret = os.environ.get("HYPERCHAT_OPERATOR_GOOGLE_CLIENT_SECRET", "").strip()
    if not client_id or not secret:
        raise GoogleAuthError("operator_google_client_not_configured")
    if client_id == os.environ.get("HYPERCHAT_GOOGLE_CLIENT_ID", "").strip():
        raise GoogleAuthError("operator_client_must_not_be_the_api_client")
    return GoogleOAuth(client_id, secret, redirect_uri="")


async def _verify_google(google: GoogleOAuth, admin_email: str, expect: str, sleep) -> tuple[str, str]:
    start = await google.device_start()
    print(f"Have {admin_email} (whom you have verified out of band) open {start['verification_url']} ON THEIR OWN "
          f"DEVICE, enter the code {start['user_code']} and sign in with {expect}. Never give this code to anyone else.",
          flush=True)
    identity = await google.device_poll(start["device_code"], start["interval"], start["expires_in"], sleep=sleep)
    if identity.email.strip().lower() != expect.strip().lower():
        raise GoogleAuthError("google_email_mismatch")
    return identity.subject, identity.email


def main(argv: list[str] | None = None, *, google: GoogleOAuth | None = None, sleep=asyncio.sleep) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.stepup.operator", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list").add_argument("--email")
    sub.add_parser("readiness")
    for name in ("approve", "revoke", "recover", "link-google"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--email", required=True)
        cmd.add_argument("--operator", required=True)
        cmd.add_argument("--confirm-database", required=True)
    for name in ("approve", "revoke"):
        sub.choices[name].add_argument("--fingerprint", required=True)
    for name in ("approve", "link-google"):
        sub.choices[name].add_argument("--verified-by", required=True,
                                       help="how the administrator's identity was confirmed out of band")
    for name in ("revoke", "recover"):
        sub.choices[name].add_argument("--reason", required=True)
    sub.choices["link-google"].add_argument("--expect-google-email", required=True,
                                            help="the Google address the verified administrator said they will use")
    draft = sub.add_parser("enforcement-draft")
    for flag in ("--reason", "--operator", "--confirm-database"):
        draft.add_argument(flag, required=True)
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
        if args.command == "enforcement-draft":
            try:
                outcome = conn.execute("SELECT public.stepup_enforcement_draft(%s, %s) AS r",
                                       (args.operator, args.reason)).fetchone()["r"]
            except psycopg.errors.Error as exc:
                return _refuse(exc.diag.message_primary or exc.sqlstate or "database error")
            if outcome == "already_draft":
                print(f"enforcement-draft: {database} is already draft; nothing changed.")
                return 0
            if outcome != "ok":
                return _refuse(outcome)
            print(f"enforcement-draft: {database} is now draft (administrator MFA no longer enforced).")
            return 0
        if args.command == "link-google":
            if not args.verified_by.strip():
                return _refuse("--verified-by must say how the administrator was verified")
            target = conn.execute("SELECT 1 FROM public.users WHERE email = %s::citext AND role = 'admin' "
                                  "AND is_active", (args.email,)).fetchone()
            if target is None:  # checked before anyone is asked to sign in with Google
                return _refuse("not an active administrator")
            try:
                subject, google_email = asyncio.run(_verify_google(google or operator_google(), args.email,
                                                                   args.expect_google_email, sleep))
            except GoogleAuthError as exc:
                return _refuse(f"Google verification failed: {exc.reason}")
            evidence = {"method": "google_device_flow", "verified_at": datetime.now(UTC).isoformat()}
        try:
            if args.command == "approve":
                outcome = conn.execute("SELECT public.stepup_approve_credential(%s, %s, %s, %s) AS r",
                                       (args.email, args.fingerprint, args.operator, args.verified_by)).fetchone()["r"]
            elif args.command == "link-google":
                outcome = conn.execute("SELECT public.admin_link_google_identity(%s, %s, %s, %s, %s, %s::jsonb) AS r",
                                       (args.email, subject, google_email, args.operator, args.verified_by,
                                        json.dumps(evidence))).fetchone()["r"]
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
        print(f"{args.command}: done for {args.email}.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
