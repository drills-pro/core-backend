"""Usage metering (req/09-usage-metering.md).

FR3's open question (inference compute-unit normalization formula) is
explicitly unresolved in the spec — we take the doc's own fallback: raw
GPU-seconds, no synthetic unit, pending a real pricing decision. That's
`PRICING_VERSION` below; bump it and every write after the bump carries the
new version, so past invoices stay reproducible from the version that was
active when the usage occurred.
"""

from app.modules.user.projects import get_team_id

PRICING_VERSION = "v1-raw-gpu-seconds"


async def record_usage(
    conn,
    *,
    tenant_id: str,
    project_id,
    resource_type: str,
    quantity,
    unit: str,
    occurred_at,
    source_ref: str,
) -> bool:
    """Idempotent append (FR1/NFR2): a repeat call with the same
    (resource_type, source_ref) no-ops. Returns True if a new row was written."""
    # FR1 (09-usage-billing-two-tier.md): team_id derived from the project
    # at write time, so call sites don't need to look it up themselves.
    team_id = await get_team_id(conn, project_id)
    row = await conn.fetchrow(
        "INSERT INTO usage_events "
        "(tenant_id, project_id, team_id, resource_type, quantity, unit, occurred_at, source_ref, pricing_version) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
        "ON CONFLICT (resource_type, source_ref) DO NOTHING "
        "RETURNING id",
        tenant_id,
        project_id,
        team_id,
        resource_type,
        quantity,
        unit,
        occurred_at,
        source_ref,
        PRICING_VERSION,
    )
    return row is not None
