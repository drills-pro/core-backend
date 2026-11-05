"""Live progress for core-api: after an epoch the root rank posts one `progress` event (planning 06 "Events").

Best effort by design. A post that fails, times out or is refused is logged and training goes on; the run's terminal state and
metrics still travel in report.json. `seq` is the epoch index, so a resume after a lost worker re-posts epochs harmlessly:
core-api keeps the first event of each seq. Nothing is sent unless the spec has `reporting`.
"""
import json
import logging
import math
import os
from numbers import Real
from urllib.request import Request, urlopen

from ray_training.constants import EVENTS_TIMEOUT_SECONDS, EventType
from ray_training.spec import TrainSpec

logger = logging.getLogger(__name__)


def _body(spec: TrainSpec, metrics: dict) -> dict:
    epoch = metrics["epoch"]  # 1-based, set by loop.fit
    scalars = {k: float(v) for k, v in metrics.items() if k != "epoch" and isinstance(v, Real) and not isinstance(v, bool) and math.isfinite(v)}
    return {"type": EventType.PROGRESS, "seq": epoch - 1, "epoch": epoch, "epochs": spec.epochs, "metrics": scalars}


def post_progress(spec: TrainSpec, metrics: dict) -> None:
    """`metrics` is the epoch's history entry ({"epoch", "train_loss", ...}). Never raises."""
    if not (spec.events_url and spec.events_token_env):
        return
    try:
        request = Request(
            spec.events_url, json.dumps(_body(spec, metrics)).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {os.environ[spec.events_token_env]}"})
        urlopen(request, timeout=EVENTS_TIMEOUT_SECONDS).close()  # noqa: S310 - the URL core-api put in the spec
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.warning("progress of epoch %s was not delivered", metrics.get("epoch"), exc_info=True)
