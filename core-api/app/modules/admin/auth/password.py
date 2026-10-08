"""The admin console's username/password sign-in: a second way into the
console beside ./zitadel.py, and a separate one — Zitadel is never asked.

The accounts (admin_accounts, 0032) live entirely in core-api's Postgres:
the username, the scrypt hash of the password, and the lockout counters.
Every account is a platform operator. core-api issues the token itself —
see issue_local_token in app/core/auth.py.

The console cannot make, disable or enable an account — that is this
module's CLI, run where core-api runs (it uses the same database
credentials). The one exception is an operator's own profile, preferences
and password, which ./account.py lets them change while signed in.

    python -m app.modules.admin.auth create alice --email alice@example.com --name "Alice"
    python -m app.modules.admin.auth set-password alice
    python -m app.modules.admin.auth disable alice
    python -m app.modules.admin.auth enable alice
    python -m app.modules.admin.auth list

Passwords are prompted for, or read from stdin with --password-stdin, so
they never sit in shell history.

scrypt is from the standard library — no extra dependency — and is one of
the password hashes OWASP lists. The stored string carries its own cost
parameters, so raising them later leaves the older hashes verifiable.
"""
import argparse
import asyncio
import base64
import binascii
import getpass
import hashlib
import hmac
import secrets
import sys
from datetime import timedelta

import asyncpg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import (
    PLATFORM_ADMIN_ROLE,
    LOCAL_SUBJECT_PREFIX,
    LOCAL_TOKEN_SECONDS,
    issue_local_token,
    local_token_secret,
)
from app.core.db import get_pool

# Mounted under /v1/admin/auth by ./routes.py.
router = APIRouter()

# OWASP's minimum for scrypt: 2^15 iterations, block size 8, parallelism 1.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
# n=2^15, r=8 needs 32 MiB, which is exactly hashlib's default ceiling and
# fails on it; headroom for that and for a higher n later.
_SCRYPT_MAXMEM = 128 * 1024 * 1024

MIN_PASSWORD_LENGTH = 12
# Wrong passwords in a row before the account stops being checked at all,
# and for how long.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT = timedelta(minutes=15)


# --- Accounts ----------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=_SCRYPT_MAXMEM, dklen=32
    )
    return "$".join(["scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P), _b64(salt), _b64(digest)])


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, binascii.Error):
        return False
    return hmac.compare_digest(actual, expected)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


_decoy: str | None = None


def _decoy_hash() -> str:
    """Checked against when there is no account to check, so a wrong
    username takes as long as a wrong password and the timing gives no
    account away. Made on first use: it costs a whole scrypt."""
    global _decoy
    if _decoy is None:
        _decoy = hash_password(secrets.token_urlsafe(16))
    return _decoy


async def authenticate(login: str, password: str) -> dict | None:
    """The account behind a username (or email) and password, or None.

    None for every way it can fail — no such account, wrong password,
    disabled, locked — so the caller cannot tell them apart either. A
    wrong password counts towards the lockout; a right one clears it.
    """
    async with (await get_pool()).acquire() as conn:
        account = await conn.fetchrow(
            "SELECT id, username, email, display_name, password_hash, "
            "       disabled OR coalesce(locked_until > now(), false) AS barred "
            "FROM admin_accounts WHERE lower(username) = lower($1) OR lower(email) = lower($1) "
            # A login that is one account's username and another's email
            # means the username: that is the one nobody else can claim.
            "ORDER BY lower(username) = lower($1) DESC LIMIT 1",
            login,
        )
        if account is None or account["barred"]:
            await asyncio.to_thread(verify_password, password, _decoy_hash())
            return None

        if not await asyncio.to_thread(verify_password, password, account["password_hash"]):
            await count_failed_attempt(conn, account["id"])
            return None

        await conn.execute(
            "UPDATE admin_accounts SET failed_attempts = 0, locked_until = NULL, last_sign_in_at = now() "
            "WHERE id = $1",
            account["id"],
        )
    return {k: account[k] for k in ("id", "username", "email", "display_name")}


async def count_failed_attempt(conn, account_id) -> None:
    """One more wrong password against an account, locking it once there
    have been enough in a row. Also counted when a signed-in operator gets
    their current password wrong while changing it (./account.py), so a
    stolen session cannot guess its way to the password either."""
    # The lock resets the count, so the first try after it ends is not
    # already the last one.
    await conn.execute(
        "UPDATE admin_accounts SET "
        "  locked_until = CASE WHEN failed_attempts + 1 >= $2 THEN now() + $3::interval ELSE locked_until END, "
        "  failed_attempts = CASE WHEN failed_attempts + 1 >= $2 THEN 0 ELSE failed_attempts + 1 END "
        "WHERE id = $1",
        account_id,
        MAX_FAILED_ATTEMPTS,
        LOCKOUT,
    )


# --- HTTP --------------------------------------------------------------------


class PasswordRequest(BaseModel):
    login_name: str
    # scrypt takes any length; this only keeps a request from making it
    # hash a megabyte.
    password: str = Field(max_length=1024)


# One message for every way the credentials can be wrong — no such
# account, wrong password, disabled, locked — so the form cannot be used
# to find out which accounts exist.
BAD_CREDENTIALS = "invalid username or password"


@router.post("/password")
async def password(body: PasswordRequest):
    """A username (or email) and password from the console's sign-in form,
    checked against admin_accounts — Zitadel takes no part.

    Answers in the shape /redeem does, so the console keeps one kind of
    session whichever way someone came in; there is no ID token, because
    there is no OIDC provider to end a session with at sign-out.
    """
    login_name = body.login_name.strip()
    if not login_name or not body.password:
        raise HTTPException(status_code=400, detail="enter a username and a password")
    # Before the password is checked, so a missing secret cannot count as
    # a wrong guess towards someone's lockout.
    local_token_secret()

    account = await authenticate(login_name, body.password)
    if account is None:
        raise HTTPException(status_code=401, detail=BAD_CREDENTIALS)

    return {
        "access_token": issue_local_token(account["username"]),
        "id_token": None,
        "expires_in": LOCAL_TOKEN_SECONDS,
        "user": {
            "sub": LOCAL_SUBJECT_PREFIX + account["username"],
            "name": account["display_name"],
            "email": account["email"],
            "login_name": account["username"],
            # Every account in admin_accounts is an operator; see 0032.
            "roles": [PLATFORM_ADMIN_ROLE],
            "org_id": None,
        },
    }


# --- CLI ---------------------------------------------------------------------


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Again: ") != password:
            sys.exit("The two passwords differ.")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"A password needs at least {MIN_PASSWORD_LENGTH} characters.")
    return password


async def _run(args: argparse.Namespace) -> str:
    async with (await get_pool()).acquire() as conn:
        if args.command == "list":
            rows = await conn.fetch(
                "SELECT username, email, display_name, disabled, locked_until > now() AS locked, last_sign_in_at "
                "FROM admin_accounts ORDER BY lower(username)"
            )
            return "\n".join(
                f"{r['username']:<24} {r['email'] or '-':<32} "
                f"{'disabled' if r['disabled'] else 'locked' if r['locked'] else 'active':<9} "
                f"last sign-in {r['last_sign_in_at'] or 'never'}"
                for r in rows
            ) or "No accounts."

        if args.command == "create":
            password_hash = hash_password(_read_password(args.password_stdin))
            try:
                await conn.execute(
                    "INSERT INTO admin_accounts (username, email, display_name, password_hash) VALUES ($1, $2, $3, $4)",
                    args.username,
                    args.email,
                    args.name,
                    password_hash,
                )
            except asyncpg.UniqueViolationError:
                sys.exit("An account already has that username or email.")
            return f"Created {args.username}."

        if args.command == "set-password":
            password_hash = hash_password(_read_password(args.password_stdin))
            sql = (
                "UPDATE admin_accounts SET password_hash = $2, password_changed_at = now(), "
                "failed_attempts = 0, locked_until = NULL, updated_at = now() WHERE lower(username) = lower($1)"
            )
            params = (args.username, password_hash)
        else:  # enable / disable; enabling also lifts a lockout
            sql = (
                "UPDATE admin_accounts SET disabled = $2, failed_attempts = 0, locked_until = NULL, "
                "updated_at = now() WHERE lower(username) = lower($1)"
            )
            params = (args.username, args.command == "disable")

        if await conn.execute(sql, *params) == "UPDATE 0":
            sys.exit(f"No account is called {args.username}.")
        return {"set-password": "Password changed", "enable": "Enabled", "disable": "Disabled"}[
            args.command
        ] + f" for {args.username}."


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.modules.admin.auth", description="The admin console's own password accounts."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="make a new account")
    create.add_argument("username")
    create.add_argument("--email")
    create.add_argument("--name", help="display name, shown in the console")
    create.add_argument("--password-stdin", action="store_true")

    set_password = commands.add_parser("set-password", help="change a password (signs its sessions out)")
    set_password.add_argument("username")
    set_password.add_argument("--password-stdin", action="store_true")

    for name, text in (("disable", "stop an account signing in, now"), ("enable", "let it sign in again")):
        commands.add_parser(name, help=text).add_argument("username")
    commands.add_parser("list", help="every account and its state")

    print(asyncio.run(_run(parser.parse_args())))
