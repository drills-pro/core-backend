async def record(conn, *, tenant_id: str, project_id, actor: str, action: str, entity_type: str, entity_id):
    """Append one audit_log row (req/03 FR3). Call inside the same
    transaction as the mutation it's recording, using an already
    tenant-scoped connection (see app.core.tenant_conn.TenantConn)."""
    await conn.execute(
        "INSERT INTO audit_log (tenant_id, project_id, actor, action, entity_type, entity_id) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        tenant_id,
        project_id,
        actor,
        action,
        entity_type,
        str(entity_id),
    )
