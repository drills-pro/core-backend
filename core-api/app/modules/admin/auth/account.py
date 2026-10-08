"""A signed-in operator's own password account: the console's profile and
settings pages (admin-frontend src/app/(console)/profile, /settings).

    GET   /v1/admin/account            profile + preferences
    PATCH /v1/admin/account/profile    display name, email, phone
    PATCH /v1/admin/account/settings   preferences (0037)
    POST  /v1/admin/account/password   change the password

Password accounts only (admin_accounts, 0032): every route takes the
caller from their own token through get_password_admin, so one operator
can never name another's account, and someone who signed in through
Zitadel — who has no row here — is turned away rather than handed an
empty profile.
"""
import asyncio
import re
from datetime import datetime, timezone
from typing import Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.core.auth import LOCAL_TOKEN_SECONDS, get_password_admin, issue_local_token
from app.core.db import get_pool
from app.modules.admin.auth.password import (
    MIN_PASSWORD_LENGTH,
    count_failed_attempt,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/v1/admin/account", tags=["admin"])

PROFILE_COLUMNS = ("username", "display_name", "email", "phone")
SETTINGS_COLUMNS = (
    "theme",
    "table_density",
    "language",
    "time_zone",
    "date_format",
    "notify_new_organization",
    "notify_deployment_failed",
    "notify_suspension",
    "weekly_digest",
    "confirm_destructive",
)
# Shown on the profile page beside the editable fields.
ACCOUNT_COLUMNS = ("created_at", "last_sign_in_at", "password_changed_at")

# Not a full RFC 5322 check — just enough to catch a typo before it
# becomes the address someone signs in with.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


class ProfileUpdate(BaseModel):
    """Only the fields sent are changed. An empty email or phone clears
    it; the display name cannot be cleared, since the console has nothing
    better to call someone than what they chose."""

    display_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=32)

    @field_validator("display_name", "email", "phone")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return _blank_to_none(value)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        if value is not None and not EMAIL_PATTERN.match(value):
            raise ValueError("not an email address")
        return value


class SettingsUpdate(BaseModel):
    """Only the fields sent are changed. The choices mirror 0037's CHECK
    constraints, so a bad value is a 422 with a reason rather than a 500
    from Postgres."""

    theme: Literal["system", "light", "dark"] | None = None
    table_density: Literal["comfortable", "compact"] | None = None
    language: Literal["en", "th"] | None = None
    # Checked against Postgres's own list in update_settings: a CHECK
    # cannot consult it (see 0037), and Python's zoneinfo depends on the
    # host having a time zone database at all.
    time_zone: str | None = Field(default=None, max_length=64)
    date_format: Literal["dd/mm/yyyy", "mm/dd/yyyy", "yyyy-mm-dd", "d mmm yyyy"] | None = None
    notify_new_organization: bool | None = None
    notify_deployment_failed: bool | None = None
    notify_suspension: bool | None = None
    weekly_digest: bool | None = None
    confirm_destructive: bool | None = None


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


@router.get("")
async def get_account(username: str = Depends(get_password_admin)):
    async with (await get_pool()).acquire() as conn:
        return _shape(await _fetch(conn, username))


@router.patch("/profile")
async def update_profile(body: ProfileUpdate, username: str = Depends(get_password_admin)):
    changes = {key: getattr(body, key) for key in body.model_fields_set}
    if "display_name" in changes and changes["display_name"] is None:
        raise HTTPException(status_code=422, detail="a display name cannot be empty")
    async with (await get_pool()).acquire() as conn:
        try:
            await _update(conn, username, changes)
        except asyncpg.UniqueViolationError:
            # The email is a way to sign in (0032), so two accounts cannot share one.
            raise HTTPException(status_code=409, detail="another account already uses that email") from None
        return _shape(await _fetch(conn, username))


@router.patch("/settings")
async def update_settings(body: SettingsUpdate, username: str = Depends(get_password_admin)):
    # None is never a stored value for a preference — every column is
    # NOT NULL — so a field sent as null is the same as one left out.
    changes = {key: getattr(body, key) for key in body.model_fields_set if getattr(body, key) is not None}
    async with (await get_pool()).acquire() as conn:
        if "time_zone" in changes and not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = $1)", changes["time_zone"]
        ):
            raise HTTPException(status_code=422, detail="not an IANA time zone name")
        await _update(conn, username, changes)
        return _shape(await _fetch(conn, username))["settings"]


@router.post("/password")
async def change_password(body: PasswordChange, username: str = Depends(get_password_admin)):
    """Change the caller's own password, given the current one.

    Moving password_changed_at signs out every other session this account
    holds (see _verify_local_token) — the caller's own included, so the
    answer carries a fresh token for the console to swap in.
    """
    if len(body.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=422, detail=f"the new password needs at least {MIN_PASSWORD_LENGTH} characters"
        )
    if body.new_password == body.current_password:
        raise HTTPException(status_code=422, detail="the new password is the same as the current one")

    async with (await get_pool()).acquire() as conn:
        account = await conn.fetchrow(
            "SELECT id, password_hash FROM admin_accounts WHERE lower(username) = lower($1)", username
        )
        if account is None:
            raise HTTPException(status_code=401, detail="this account can no longer sign in")
        if not await asyncio.to_thread(verify_password, body.current_password, account["password_hash"]):
            await count_failed_attempt(conn, account["id"])
            # 403, not 401: the session is fine, and a 401 would read to
            # the console as having been signed out.
            raise HTTPException(status_code=403, detail="the current password is wrong")

        new_hash = await asyncio.to_thread(hash_password, body.new_password)
        # Whole seconds, taken before the token is issued: a token is
        # refused when its `iat` (whole seconds) is older than this, so the
        # fresh one has to land in the same second or a later one — which
        # Postgres's now(), with its fractions and its own clock, would
        # not promise.
        changed_at = datetime.now(timezone.utc).replace(microsecond=0)
        await conn.execute(
            "UPDATE admin_accounts SET password_hash = $2, password_changed_at = $3, "
            "failed_attempts = 0, locked_until = NULL, updated_at = now() WHERE id = $1",
            account["id"],
            new_hash,
            changed_at,
        )

    return {"access_token": issue_local_token(username), "expires_in": LOCAL_TOKEN_SECONDS}


async def _fetch(conn, username: str):
    row = await conn.fetchrow(
        f"SELECT {', '.join(PROFILE_COLUMNS + SETTINGS_COLUMNS + ACCOUNT_COLUMNS)} "
        "FROM admin_accounts WHERE lower(username) = lower($1)",
        username,
    )
    if row is None:
        # get_password_admin read it a moment ago; it went in between.
        raise HTTPException(status_code=401, detail="this account can no longer sign in")
    return row


async def _update(conn, username: str, changes: dict) -> None:
    if not changes:
        return
    # Column names come from the request models' own field names, never
    # from the request body, so they are safe to put into the SQL.
    assignments = ", ".join(f"{column} = ${i}" for i, column in enumerate(changes, start=2))
    await conn.execute(
        f"UPDATE admin_accounts SET {assignments}, updated_at = now() WHERE lower(username) = lower($1)",
        username,
        *changes.values(),
    )


def _shape(row) -> dict:
    return {
        **{column: row[column] for column in PROFILE_COLUMNS + ACCOUNT_COLUMNS},
        "settings": {column: row[column] for column in SETTINGS_COLUMNS},
    }
