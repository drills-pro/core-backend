from app.core.authz import AuthzContext
from app.core.enums import CatalogStatus
from app.core.tenant_conn import TenantConn
from app.modules.user.catalog.schemas import CatalogModel

# model_catalog is platform data (no RLS), so every tenant reads the same enabled rows (docs 17, catalog).
_MODELS = (
    "SELECT id, repo_id, revision, display_name, task, license, params, size_bytes FROM model_catalog "
    "WHERE status = $1 AND ($2::text IS NULL OR task = $2) ORDER BY task, display_name, created_at"
)


async def list_models(ctx: AuthzContext, task: str | None) -> list[CatalogModel]:
    async with TenantConn(ctx.tenant_id) as conn:
        return [CatalogModel.model_validate(dict(r)) for r in await conn.fetch(_MODELS, CatalogStatus.ENABLED, task)]
