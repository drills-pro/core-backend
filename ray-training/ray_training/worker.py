"""What runs inside a Ray Train worker: the loop of ray_training.loop with data parallelism, one checkpoint and report per
epoch, and resuming from the last checkpoint. Imports torch, so only the GPU workers and the driver's image load it.

Every rank loads the dataset and trains; the root rank alone evaluates, exports and uploads (the trainer returns None
on the others). An exception on any rank ends the attempt for all of them, which is what keeps the others out of a
collective that one rank will never reach.
"""
import json
import tempfile
from pathlib import Path

import ray.train
import ray.train.torch
import torch
from ray.train import Checkpoint

from ray_training import loop
from ray_training.constants import ROOT_RANK, STATE_FILE
from ray_training.data import load_dataset
from ray_training.registry import trainer_for
from ray_training.runner import upload
from ray_training.spec import parse_spec
from ray_training.store import store_from_env

REPORT_FIELD = "report"  # the final report's key in the metrics the root rank reports
REPORT_FILE = "final.json"  # marks the last checkpoint, which holds no state


class RaySession(loop.Session):
    def __init__(self):
        ctx = ray.train.get_context()
        self.rank, self.world_size = ctx.get_world_rank(), ctx.get_world_size()

    def prepare_model(self, model):
        return ray.train.torch.prepare_model(model)

    def prepare_loader(self, loader):
        return ray.train.torch.prepare_data_loader(loader)

    def resume(self):
        checkpoint = ray.train.get_checkpoint()
        if checkpoint is None:
            return None
        with checkpoint.as_directory() as d:
            return torch.load(Path(d) / STATE_FILE, map_location="cpu")

    def report(self, metrics, state):
        if self.rank != ROOT_RANK:
            ray.train.report({})
            return
        with tempfile.TemporaryDirectory() as d:
            torch.save(state(), Path(d) / STATE_FILE)
            ray.train.report(metrics, checkpoint=Checkpoint.from_directory(d))


def train_loop(config: dict) -> None:
    """The `train_loop_per_worker` of the driver's TorchTrainer; config["spec"] is the job spec JSON."""
    spec = parse_spec(config["spec"])
    session = RaySession()
    loop.use(session)
    report = None
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        result = trainer_for(spec.trainer)(spec, load_dataset(spec, work), work)
        if result is not None:
            report = upload(spec, store_from_env(spec.bucket, spec.prefix), result)
    if report is None:
        ray.train.report({})
        return
    with tempfile.TemporaryDirectory() as d:  # Result.metrics are those of the latest checkpointed report, so the final one carries one
        Path(d, REPORT_FILE).write_text("{}")
        ray.train.report({REPORT_FIELD: json.dumps(report, default=str)}, checkpoint=Checkpoint.from_directory(d))
