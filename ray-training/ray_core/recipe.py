"""The parts of a training recipe the spec pins per preset: learning-rate schedule, train-time augmentation, early stopping."""
import math
import random
from typing import Callable

import torch
import torch.nn.functional as F

from ray_core.constants import CROP_PAD_DIVISOR, FLIP_PROBABILITY, LrSchedule


def lr_factor(schedule: LrSchedule, warmup_fraction: float, total_steps: int) -> Callable[[int], float]:
    """Multiplier of the learning rate at optimizer step `step`: linear warmup, then held or cosine-decayed to zero."""
    warm = int(warmup_fraction * total_steps)

    def factor(step: int) -> float:
        if step < warm:
            return (step + 1) / warm
        if schedule == LrSchedule.CONSTANT:
            return 1.0
        return 0.5 * (1 + math.cos(math.pi * min(step - warm, total_steps - warm) / max(total_steps - warm, 1)))

    return factor


def make_scheduler(optimizer, schedule: LrSchedule, warmup_fraction: float, total_steps: int):
    return torch.optim.lr_scheduler.LambdaLR(optimizer, lr_factor(schedule, warmup_fraction, total_steps))


def flip_crop(image: torch.Tensor) -> torch.Tensor:
    """CHW: random horizontal flip, then a random crop of the reflect-padded image back to its own size."""
    if random.random() < FLIP_PROBABILITY:
        image = image.flip(-1)
    pad = image.shape[-1] // CROP_PAD_DIVISOR
    padded = F.pad(image.unsqueeze(0), (pad,) * 4, mode="reflect").squeeze(0)
    top, left = random.randint(0, 2 * pad), random.randint(0, 2 * pad)
    return padded[:, top:top + image.shape[-2], left:left + image.shape[-1]]


def flip_with_boxes(image: torch.Tensor, boxes: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """CHW image and normalised (cx, cy, w, h) boxes: a horizontal flip mirrors cx and leaves the rest."""
    if random.random() >= FLIP_PROBABILITY:
        return image, boxes
    boxes = boxes.clone()
    boxes[:, 0] = 1 - boxes[:, 0]
    return image.flip(-1), boxes


class EarlyStopping:
    """Stops after `patience` epochs without a better value of a higher-is-better metric; patience 0 never stops.
    The model kept is the last one trained, not the best (the best is not stored)."""

    def __init__(self, patience: int):
        self.patience, self.best, self.bad = patience, -math.inf, 0

    @property
    def stopped(self) -> bool:
        return self.patience > 0 and self.bad >= self.patience

    def update(self, value: float) -> bool:
        if value > self.best:
            self.best, self.bad = value, 0
        else:
            self.bad += 1
        return self.stopped

    def state_dict(self) -> dict:
        return {"best": self.best, "bad": self.bad}

    def load_state_dict(self, state: dict) -> None:
        self.best, self.bad = state["best"], state["bad"]
