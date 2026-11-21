"""What an admin may do to the training presets. `admitted` runs before a connection is taken (the Hub is a network call);
`put` and `delete` take the connection and are each one transaction: the change and its admin_audit row land or neither does."""
from http import HTTPStatus

from app.core import admin_audit, presets, task_types
from app.core.enums import AdminAuditAction, AdminAuditEntity, TaskType
from app.core.presets import PresetBody, TaskPreset
from app.core.problem import Problem, not_found
from app.core.settings import settings
from app.integrations import hub
from app.modules.admin.presets.constants import ProblemCode


async def _trainable(conn, task: TaskType) -> TaskType:
    info = await task_types.get(conn, task)
    if info is None or not info.trainable:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.TASK_NOT_TRAINABLE, f"task type '{task}' has no trainer, so it has no presets")
    return task


async def _admit(body: PresetBody) -> PresetBody:
    """Check the pinned base model against the admission policy and record the licence the Hub declares for it."""
    base = body.base_model
    try:
        facts = await hub.fetch(base.repo_id, base.revision)
    except hub.HubUnavailable as e:
        raise Problem(HTTPStatus.BAD_GATEWAY, ProblemCode.HUB_UNAVAILABLE, str(e))
    if reasons := hub.refusals(facts):
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.MODEL_NOT_ADMITTED, f"{base.repo_id}: " + "; ".join(reasons))
    return body.model_copy(update={"base_model": base.model_copy(update={"license": facts.license})})


async def admitted(task: TaskType, body: PresetBody) -> PresetBody:
    """The body an admin may write for `task`: a recipe its trainer implements, and, with a Hub configured, a base model the
    admission policy accepts (the body then carries the licence the Hub declares). No connection is held."""
    try:
        body.check_task(task)
    except ValueError as e:
        raise Problem(HTTPStatus.UNPROCESSABLE_ENTITY, ProblemCode.INVALID_RECIPE, str(e))
    return await _admit(body) if settings.training.hub_url else body


async def list_for(conn, task: TaskType) -> list[TaskPreset]:
    return await presets.list_for(conn, await _trainable(conn, task))


async def put(conn, task: TaskType, name: str, body: PresetBody, actor: str) -> TaskPreset:
    """Create or replace one preset of a task that has a trainer. Runs already started keep the values they were given."""
    async with conn.transaction():
        row = await presets.put(conn, await _trainable(conn, task), name, body, actor)
        await admin_audit.record(conn, actor, AdminAuditAction.TASK_PRESET_PUT, AdminAuditEntity.TASK_PRESET, f"{task}/{name}", body.model_dump(mode="json"))
    return row


async def delete(conn, task: TaskType, name: str, actor: str) -> None:
    async with conn.transaction():
        if not await presets.delete(conn, await _trainable(conn, task), name):
            raise not_found()
        await admin_audit.record(conn, actor, AdminAuditAction.TASK_PRESET_DELETE, AdminAuditEntity.TASK_PRESET, f"{task}/{name}")
