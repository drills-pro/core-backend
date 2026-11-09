"""What a key may do with a prediction it made: read it (planning/24). A key sees only its own, in its own project; a miss, another key's
prediction and a malformed id are the same 404."""
import uuid

from app.core.authz import KeyContext
from app.core.problem import not_found
from app.core.tenant_conn import TenantConn
from app.modules.user.deployments import prediction_store as store
from app.modules.user.deployments.constants import PREDICTION_ID_PREFIX
from app.modules.user.deployments.schemas import PredictionEnvelope

_OWNED = "SELECT * FROM predictions WHERE id = $1 AND api_key_id = $2 AND project_id = $3"


def parse_id(prediction_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(prediction_id.removeprefix(PREDICTION_ID_PREFIX))
    except ValueError:
        raise not_found() from None


async def owned(conn, ctx: KeyContext, prediction_id: str):
    """The row of `prediction_id` when this key made it in this project; the one 404 otherwise."""
    row = await conn.fetchrow(_OWNED, parse_id(prediction_id), ctx.key_id, ctx.project_id)
    if row is None:
        raise not_found()
    return row


async def get(ctx: KeyContext, prediction_id: str) -> PredictionEnvelope:
    async with TenantConn(ctx.tenant_id) as conn:
        return store.envelope(await owned(conn, ctx, prediction_id))
