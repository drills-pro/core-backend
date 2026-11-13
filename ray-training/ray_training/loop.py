"""The training loop every trainer shares: epochs, evaluation, and, inside a Ray Train worker, data parallelism, a
checkpoint and a report per epoch, and resuming from the last checkpoint after a worker was lost.

A trainer describes its task as a `Kit` and calls `fit`. Outside Ray Train (tests, a local run) the default `Session`
does nothing extra and `fit` is a plain single-process loop; ray_training.worker installs the Ray one.
"""
from dataclasses import dataclass
from typing import Any, Callable

import torch
from torch.utils.data import DataLoader

from ray_training.constants import ROOT_RANK
from ray_training.recipe import EarlyStopping, make_scheduler
from ray_training.result import NUM_WORKERS_GPU, cap_gpu_share, device
from ray_training.spec import TrainSpec

Batch = Any
EvaluateFn = Callable[[torch.nn.Module, DataLoader, torch.device], tuple[dict, Any]]  # (metrics, confusion matrix or None)


@dataclass
class Kit:
    """What differs between tasks. `loss` runs a forward pass on one batch and returns (loss, number of samples)."""

    model: torch.nn.Module
    train_set: Any
    eval_set: Any
    collate: Callable | None
    loss: Callable[[torch.nn.Module, Batch, torch.device], tuple[torch.Tensor, int]]
    evaluate: EvaluateFn
    monitor: str = "top1"  # the evaluate() metric early stopping watches, higher is better


@dataclass
class Fit:
    model: torch.nn.Module  # unwrapped, on the root rank only
    metrics: dict
    confusion: Any
    history: list[dict]


class Session:
    """Where a run is executed: one process here; ray_training.worker.RaySession inside a Ray Train worker."""

    rank = ROOT_RANK
    world_size = 1

    def device(self) -> torch.device:
        return device()

    def prepare_model(self, model: torch.nn.Module) -> torch.nn.Module:
        return model

    def prepare_loader(self, loader: DataLoader) -> DataLoader:
        return loader

    def resume(self) -> dict | None:
        """The state saved with the last checkpoint, or None to start from scratch."""
        return None

    def agree(self, stop: bool) -> bool:
        """Every rank calls it once per epoch with its own verdict (only the root rank's is real); all get the root's."""
        return stop

    def report(self, metrics: dict, state: Callable[[], dict]) -> None:
        """Called by every rank at the end of an epoch; `state()` is what a resumed run needs (called on the root rank only)."""


session = Session()


def use(new: Session) -> None:
    global session
    session = new


def _unwrapped(model: torch.nn.Module) -> torch.nn.Module:
    return getattr(model, "module", model)  # DistributedDataParallel keeps the model here


def _cpu_state(module: torch.nn.Module) -> dict:
    return {k: v.detach().cpu() for k, v in module.state_dict().items()}


def fit(kit: Kit, spec: TrainSpec) -> Fit | None:
    """Train `kit.model` for spec.epochs and evaluate it. Returns None on every rank but the root one, which owns the export."""
    dev = session.device()
    cap_gpu_share(getattr(spec, "gpu", 0), getattr(spec, "vram_gb", None))  # the test doubles carry no GPU share: uncapped
    model = session.prepare_model(kit.model.to(dev))
    inner = _unwrapped(model)
    workers = NUM_WORKERS_GPU if dev.type == "cuda" else 0
    train_loader = session.prepare_loader(
        DataLoader(kit.train_set, spec.batch_size, shuffle=True, collate_fn=kit.collate, num_workers=workers))
    eval_loader = DataLoader(kit.eval_set, spec.batch_size, collate_fn=kit.collate, num_workers=workers)  # root rank only, whole set
    optimizer = torch.optim.AdamW(model.parameters(), lr=spec.learning_rate)
    scheduler = make_scheduler(optimizer, spec.lr_schedule, spec.warmup_fraction, spec.epochs * len(train_loader))  # stepped per batch
    stopper = EarlyStopping(spec.early_stopping_patience)
    history: list[dict] = []
    start = 0
    if (saved := session.resume()) is not None:
        inner.load_state_dict(saved["model"])
        optimizer.load_state_dict(saved["optimizer"])
        scheduler.load_state_dict(saved["scheduler"])
        stopper.load_state_dict(saved["stopper"])
        history, start = saved["history"], saved["epoch"]
    stop = stopper.stopped  # read from the same checkpoint on every rank, so they agree
    metrics, confusion = {}, None
    for epoch in range(start, spec.epochs):
        if stop:
            break
        if hasattr(train_loader.sampler, "set_epoch"):
            train_loader.sampler.set_epoch(epoch)  # a different shuffle each epoch on every rank
        model.train()
        total, seen = 0.0, 0
        for batch in train_loader:
            loss, count = kit.loss(model, batch, dev)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            scheduler.step()
            total += loss.item() * count
            seen += count
        if session.rank == ROOT_RANK:
            metrics, confusion = kit.evaluate(inner, eval_loader, dev)
            history.append({"epoch": epoch + 1, "train_loss": total / max(seen, 1), **metrics})
            stopper.update(metrics[kit.monitor])
        stop = session.agree(stopper.stopped)  # at an epoch boundary, so no rank is left in a collective of the next epoch
        session.report(
            history[-1] if history else {},
            lambda: {"epoch": epoch + 1, "model": _cpu_state(inner), "optimizer": optimizer.state_dict(), "history": history,
                     "scheduler": scheduler.state_dict(), "stopper": stopper.state_dict()})
    if session.rank != ROOT_RANK:
        return None
    if not metrics:  # resumed after the last epoch or after an early stop (the export was what failed): score the restored model once
        metrics, confusion = kit.evaluate(inner, eval_loader, dev)
    return Fit(inner, metrics, confusion, history)
