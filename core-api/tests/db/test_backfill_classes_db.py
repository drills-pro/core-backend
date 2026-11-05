"""The classes backfill's SQL on a real Postgres: which runs it picks, and that its write merges and is idempotent."""
import json

from app.core.enums import TaskType
from app.modules.user.training import backfill_classes as bf
from app.modules.user.training.constants import TrainingStatus

SET = "UPDATE training_runs SET task = $2, status = $3, report = $4::jsonb WHERE tenant_id = $1"
PICK = "SELECT id FROM runs_missing_report_classes($1, NULL, 10)"


def test_only_succeeded_detection_runs_without_classes_are_picked_and_the_merge_keeps_the_report(two_tenants):
    t = two_tenants

    async def go():
        report = json.dumps({"metrics": {"map": 0.5}})
        await t.conn.execute(SET, t.a, TaskType.OBJECT_DETECTION, TrainingStatus.SUCCEEDED, report)
        run = await t.conn.fetchval("SELECT id FROM training_runs WHERE tenant_id = $1", t.a)
        picked = [r["id"] for r in await t.conn.fetch(PICK, TaskType.OBJECT_DETECTION)]
        assert run in picked
        assert not await t.conn.fetch(PICK, TaskType.IMAGE_CLASSIFICATION)

        await t.conn.execute(bf._MERGE, run, json.dumps(["cat"]), TrainingStatus.SUCCEEDED)
        await t.conn.execute(bf._MERGE, run, json.dumps(["dog"]), TrainingStatus.SUCCEEDED)  # second run changes nothing
        assert json.loads(await t.conn.fetchval("SELECT report FROM training_runs WHERE id = $1", run)) == {
            "metrics": {"map": 0.5}, "classes": ["cat"]}
        assert run not in [r["id"] for r in await t.conn.fetch(PICK, TaskType.OBJECT_DETECTION)]

    t.run(go())


def test_a_failed_run_is_not_picked(two_tenants):
    t = two_tenants

    async def go():
        await t.conn.execute(SET, t.a, TaskType.OBJECT_DETECTION, TrainingStatus.FAILED, "{}")
        run = await t.conn.fetchval("SELECT id FROM training_runs WHERE tenant_id = $1", t.a)
        assert run not in [r["id"] for r in await t.conn.fetch(PICK, TaskType.OBJECT_DETECTION)]

    t.run(go())
