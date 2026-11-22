"""Two sources per row:

    admin_activity()  the event, plus the tenant and project it names
                      (Postgres, SECURITY DEFINER: audit_log is
                      RLS-protected and this connection sets no
                      app.tenant_id, so a direct SELECT returns nothing)
    Zitadel           who the actor is (audit_log stores the subject id,
                      and Zitadel owns the name behind it)

Zitadel is enrichment, not a dependency: if it is unreachable the log
still answers, with actor_name=null.
"""
import asyncio
import datetime
import uuid

from app.core.db import get_pool
from app.core.pagination import Page, PageParams, build_page, cursor_args, total_of
from app.integrations import zitadel
from app.integrations.zitadel import OrgUser
from app.modules.admin.activity.schemas import ActivityItem, ActivityStats
from app.modules.user.tenants import get_org_ids

_ACTIVITY = "SELECT * FROM admin_activity($1, $2, $3, $4, $5, $6, $7)"
_STATS = "SELECT * FROM admin_activity_stats()"


async def list_activity(
    q: str | None, tenant_id: uuid.UUID | None, entity_type: str | None, days: int | None, page: PageParams
) -> tuple[Page[ActivityItem], int]:
    """One page of every recorded action, newest first, and the size of the filtered set."""
    since = None
    if days is not None:
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)

    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch(_ACTIVITY, page.page_size + 1, *cursor_args(page.page_token), tenant_id, entity_type, q, since)
        orgs = await _org_ids(conn, {row["tenant_id"] for row in rows})

    names = await _actor_names(orgs)
    named = [dict(row, actor_name=names.get((row["tenant_id"], row["actor"]))) for row in rows]
    return build_page(named, page.page_size, ActivityItem), total_of(rows)


async def summary() -> ActivityStats:
    """`events_today` counts the database's day — the clock that stamped the
    rows — so it always agrees with what the listing groups under Today."""
    async with (await get_pool()).acquire() as conn:
        return ActivityStats.model_validate(dict(await conn.fetchrow(_STATS)))


async def _org_ids(conn, tenant_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """{tenant_id: zitadel_org_id} for the tenants this page names.

    tenants is the one table here with no RLS on it (init migration), so this is a
    plain SELECT rather than another SECURITY DEFINER hop.
    """
    if not tenant_ids:
        return {}
    return await get_org_ids(conn, list(tenant_ids))


async def _actor_names(orgs: dict[uuid.UUID, str]) -> dict[tuple[uuid.UUID, str], str]:
    """{(tenant_id, subject): display name} for everyone in those orgs.

    Keyed by tenant as well as subject because the same person can hold
    accounts in several orgs, and it is that org's Zitadel that names them.
    Failures are per-org and silent: `zitadel.list_org_users` already logs
    them and answers {}, which leaves those rows with their subject id.
    """
    if not orgs:
        return {}

    semaphore = asyncio.Semaphore(zitadel.FANOUT_CONCURRENCY)
    async with zitadel.client() as client:

        async def one(org_id: str) -> dict[str, OrgUser]:
            async with semaphore:
                return await zitadel.list_org_users(client, org_id)

        results = await asyncio.gather(*(one(org_id) for org_id in orgs.values()))

    return {
        (tenant_id, subject): user.display_name
        for (tenant_id, _), users in zip(orgs.items(), results)
        for subject, user in users.items()
        if user.display_name
    }
