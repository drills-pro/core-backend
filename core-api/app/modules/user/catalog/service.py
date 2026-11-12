from app.core.authz import AuthzContext
from app.core.tenant_conn import TenantConn
from app.modules.user.catalog.schemas import CatalogModel

# The curated list is the platform's presets, grouped by the pinned base model (docs 17, catalog; the mirror is deferred).
_MODELS = (
    "SELECT base_model_repo AS repo_id, base_model_revision AS revision, base_model_license AS license, "
    "array_agg(DISTINCT task ORDER BY task) AS tasks, array_agg(name ORDER BY name) AS presets "
    "FROM task_presets GROUP BY 1, 2, 3 ORDER BY 1, 2"
)


async def list_models(ctx: AuthzContext) -> list[CatalogModel]:
    async with TenantConn(ctx.tenant_id) as conn:
        return [CatalogModel.model_validate(dict(r)) for r in await conn.fetch(_MODELS)]
