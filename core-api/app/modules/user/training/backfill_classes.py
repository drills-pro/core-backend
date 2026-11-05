"""One-off backfill: copy `classes` from a run's model.json into its report.

A run reported before the trainer wrote a top-level `classes` has none, and an object-detection model cannot be
deployed without them (readers.RunModel.servable). The classes are in the run's model.json, which the trainer stored
next to the report. Safe to run again: a run that has `classes` is never selected, and a write only happens while the
report still lacks them.

    python -m app.modules.user.training.backfill_classes [--dry-run] [--batch-size N] [--max-runs N]

It reads and writes through the app's own database and storage credentials, like the worker. --dry-run reads and
reports, writes nothing. A run that fails (no model.json, storage error) is logged and skipped; the rest go on.
"""
import argparse
import asyncio
import json
import logging
import uuid

from pydantic import BaseModel

from app.core.db import get_pool
from app.core.enums import TaskType
from app.core.tenant_conn import TenantConn
from app.integrations import data_plane
from app.modules.user.training.constants import TrainingStatus

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 50
CLASSES_KEY = "classes"
_CANDIDATES = "SELECT tenant_id, project_id, id FROM runs_missing_report_classes($1, $2, $3)"
_MERGE = (
    "UPDATE training_runs SET report = report || jsonb_build_object('classes', $2::jsonb) "
    "WHERE id = $1 AND status = $3 AND NOT report ? 'classes'"
)


class Candidate(BaseModel):
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    id: uuid.UUID


class Summary(BaseModel):
    updated: int = 0  # with --dry-run: how many would be
    skipped: int = 0  # no usable classes in the run's model.json
    failed: int = 0


async def _candidates(task: TaskType, after: uuid.UUID | None, limit: int) -> list[Candidate]:
    async with (await get_pool()).acquire() as conn:
        return [Candidate.model_validate(dict(r)) for r in await conn.fetch(_CANDIDATES, task, after, limit)]


async def _backfill_one(run: Candidate, dry_run: bool) -> bool:
    """True when the run got (or in a dry run would get) its classes, False when model.json has none to give."""
    meta = await data_plane.read_model_meta(str(run.tenant_id), str(run.project_id), str(run.id))
    classes = (meta or {}).get(CLASSES_KEY)
    if not isinstance(classes, list) or not classes or not all(isinstance(c, str) for c in classes):
        return False
    if not dry_run:
        async with TenantConn(str(run.tenant_id)) as conn:
            await conn.execute(_MERGE, run.id, json.dumps(classes), TrainingStatus.SUCCEEDED)
    return True


async def run(*, dry_run: bool = False, batch_size: int = DEFAULT_BATCH_SIZE, max_runs: int | None = None) -> Summary:
    """Walk the succeeded object-detection runs without `classes` in id order, batch by batch, until none are left or
    `max_runs` have been looked at. The cursor moves on whatever happens to a run, so a run that cannot be fixed is not retried."""
    summary, seen, after = Summary(), 0, None
    while max_runs is None or seen < max_runs:
        limit = batch_size if max_runs is None else min(batch_size, max_runs - seen)
        batch = await _candidates(TaskType.OBJECT_DETECTION, after, limit)
        if not batch:
            break
        for candidate in batch:
            try:
                if await _backfill_one(candidate, dry_run):
                    summary.updated += 1
                else:
                    summary.skipped += 1
                    logger.warning("run %s: model.json has no classes", candidate.id)
            except Exception:  # noqa: BLE001 - one bad run must not stop the rest
                summary.failed += 1
                logger.exception("run %s: backfill failed", candidate.id)
        seen += len(batch)
        after = batch[-1].id
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="read model.json, report what would change, write nothing")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-runs", type=int, default=None, help="stop after looking at this many runs")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    summary = asyncio.run(run(dry_run=args.dry_run, batch_size=args.batch_size, max_runs=args.max_runs))
    logger.info("%s%s", "dry run: " if args.dry_run else "", summary.model_dump_json())


if __name__ == "__main__":
    main()
