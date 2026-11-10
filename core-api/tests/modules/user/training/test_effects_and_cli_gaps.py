"""effects when Ray or MLflow cannot answer, and the backfill's command line."""
import asyncio
import uuid

from app.modules.user.training import backfill_classes as bf
from app.modules.user.training import effects

ROW = {"id": uuid.uuid4(), "project_id": uuid.uuid4(), "mlflow_run_id": "mlflow-1", "ray_job_id": "train-1"}


def test_a_ray_that_cannot_say_when_the_job_started_means_nothing_is_billed(monkeypatch):
    async def job_state(job_id):
        raise RuntimeError("ray down")

    monkeypatch.setattr(effects.compute, "job_state", job_state)
    assert asyncio.run(effects.ray_started_at(ROW)) is None


def test_a_run_without_an_mlflow_run_registers_no_model_version(monkeypatch):
    async def register(*a):
        raise AssertionError("must not reach MLflow")

    monkeypatch.setattr(effects, "register_in_mlflow", register)
    assert asyncio.run(effects.register_model("t", {**ROW, "mlflow_run_id": None})) is None


def _main(monkeypatch, argv):
    seen = {}

    async def fake_run(**kw):
        seen.update(kw)
        return bf.Summary()

    monkeypatch.setattr(bf, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["backfill", *argv])
    bf.main()
    return seen


def test_the_command_line_passes_its_flags_to_the_backfill(monkeypatch):
    seen = _main(monkeypatch, ["--dry-run", "--batch-size", "7", "--max-runs", "3"])
    assert seen == {"dry_run": True, "batch_size": 7, "max_runs": 3}


def test_the_command_line_defaults_to_a_real_run_without_a_cap(monkeypatch):
    assert _main(monkeypatch, []) == {"dry_run": False, "batch_size": bf.DEFAULT_BATCH_SIZE, "max_runs": None}
