"""Projects import tenant_members, so tenant_members cannot call projects: projects registers its cleanup here when
it is imported, and removing a member with nothing registered fails closed (it would leave project bindings behind)."""
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

MemberRemoved = Callable[[Any, uuid.UUID | str, str], Awaitable[None]]
_member_removed: MemberRemoved | None = None


def register_member_removed(hook: MemberRemoved) -> None:
    global _member_removed
    _member_removed = hook


async def member_removed(conn, tenant_id: uuid.UUID | str, user_sub: str) -> None:
    if _member_removed is None:
        raise RuntimeError("no member-removed hook is registered")
    await _member_removed(conn, tenant_id, user_sub)
