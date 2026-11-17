"""LR schedule, augmentation and early stopping: the pieces (ray_core.recipe) and how loop.fit uses them. CPU, no Ray.
How each task applies its augmentation is tested in its bundle."""
import copy
import json
import math
from types import SimpleNamespace

import pytest
import torch

from ray_core import loop
from ray_core.constants import Augmentation, LrSchedule
from ray_core.loop import Kit, Session, fit
from ray_core.recipe import EarlyStopping, flip_crop, flip_with_boxes, lr_factor
from ray_core.spec import parse_spec
from tests.support import classification_rows, spec_doc

LR = 0.1


def test_constant_schedule_with_warmup_ramps_then_holds():
    f = lr_factor(LrSchedule.CONSTANT, 0.25, 8)  # 2 warmup steps
    assert [f(s) for s in range(4)] == [0.5, 1.0, 1.0, 1.0]


def test_cosine_schedule_decays_to_zero():
    f = lr_factor(LrSchedule.COSINE, 0.0, 10)
    assert f(0) == 1.0 and f(5) == pytest.approx(0.5) and f(10) == pytest.approx(0.0, abs=1e-9)
    assert [f(s) for s in range(11)] == sorted((f(s) for s in range(11)), reverse=True)


def test_flip_with_boxes_mirrors_cx_only():
    image = torch.arange(12.0).reshape(1, 3, 4)
    boxes = torch.tensor([[0.25, 0.5, 0.1, 0.2]])
    flips = 0
    for _ in range(40):
        out_img, out_boxes = flip_with_boxes(image, boxes)
        if torch.equal(out_img, image.flip(-1)):
            flips += 1
            assert out_boxes[0].tolist() == pytest.approx([0.75, 0.5, 0.1, 0.2])
        else:
            assert torch.equal(out_img, image) and torch.equal(out_boxes, boxes)
    assert 0 < flips < 40 and boxes[0, 0] == 0.25  # the input is never changed


def test_flip_crop_keeps_the_shape():
    assert flip_crop(torch.rand(3, 32, 32)).shape == (3, 32, 32)


def test_early_stopping_counts_epochs_without_improvement_and_zero_is_off():
    s = EarlyStopping(2)
    assert [s.update(v) for v in (0.5, 0.4, 0.6, 0.6, 0.5)] == [False, False, False, False, True]
    off = EarlyStopping(0)
    assert not any(off.update(0.0) for _ in range(5))
    again = EarlyStopping(2)
    again.load_state_dict(s.state_dict())
    assert again.stopped


# ---- loop.fit ----

class Tiny(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.w = torch.nn.Linear(2, 2)

    def forward(self, x):
        return self.w(x)


def kit(accuracies, seen_lrs=None, model=None):
    """A Kit whose evaluate() returns the given top1 per epoch; loss() records the optimizer's lr when asked."""
    model = model or Tiny()
    data = [(torch.randn(2), 0) for _ in range(4)]
    evals = iter(accuracies)

    def loss(m, batch, dev):
        x, y = batch
        return torch.nn.functional.cross_entropy(m(x), y), len(y)

    def evaluate(m, loader, dev):
        return {"top1": next(evals)}, None

    return Kit(model, data, data, None, loss, evaluate)


def spec(**over):
    base = dict(epochs=5, batch_size=2, learning_rate=LR, lr_schedule=LrSchedule.CONSTANT, warmup_fraction=0.0, early_stopping_patience=0,
                hyperparameters={})
    return SimpleNamespace(**{**base, **over})


@pytest.fixture(autouse=True)
def plain_session():
    loop.use(Session())
    yield
    loop.use(Session())


def test_early_stopping_ends_training_after_patience_and_keeps_the_best_model():
    done = fit(kit([0.9, 0.8, 0.7, 0.6, 0.5]), spec(early_stopping_patience=2))
    assert [h["epoch"] for h in done.history] == [1, 2, 3]  # epoch 1 best, 2 and 3 no better: stop
    assert done.metrics == {"top1": 0.9}  # the best epoch's metrics, the model the export uses


def test_the_exported_weights_are_the_best_epochs_not_the_last_trained():
    k = kit([0.9, 0.8, 0.7])
    seen, evaluate = [], k.evaluate

    def recording(m, loader, dev):
        seen.append({n: t.clone() for n, t in m.state_dict().items()})
        return evaluate(m, loader, dev)

    k.evaluate = recording
    done = fit(k, spec(epochs=3, early_stopping_patience=2))
    assert len(done.history) == 3  # epochs 2 and 3 trained after the best, then the weights went back to epoch 1's
    assert all(torch.equal(done.model.state_dict()[n], seen[0][n]) for n in seen[0])


def test_without_patience_every_epoch_runs_and_the_last_model_is_kept():
    done = fit(kit([0.9, 0.8, 0.7, 0.6, 0.5]), spec())
    assert len(done.history) == 5 and done.metrics == {"top1": 0.5}


class Recording(Session):
    """Saves state like Ray would and can hand it back to a second fit()."""

    def __init__(self, saved=None):
        self.saved, self.reports, self.lrs = saved, [], []

    def resume(self):
        return self.saved

    def report(self, metrics, state):
        self.reports.append(metrics)
        self.saved = state()


def test_the_scheduler_steps_per_batch_and_resume_restores_it():
    rec = Recording()
    loop.use(rec)
    s = spec(epochs=4, lr_schedule=LrSchedule.COSINE)
    first = kit([0.1] * 4)
    # run only two epochs: same spec but the checkpoint after epoch 2 is what we keep
    snapshots = []
    original = rec.report
    rec.report = lambda m, st: (original(m, st), snapshots.append(copy.deepcopy(rec.saved)))
    fit(first, s)
    mid = snapshots[1]  # after epoch 2 of 4: 2 batches per epoch, 4 steps taken
    assert mid["scheduler"]["last_epoch"] == 4 and mid["epoch"] == 2

    rec2 = Recording(mid)
    loop.use(rec2)
    done = fit(kit([0.2, 0.3], model=Tiny()), s)
    assert [h["epoch"] for h in done.history] == [1, 2, 3, 4]  # history carried over, two epochs run now
    assert rec2.saved["scheduler"]["last_epoch"] == 8  # continued from step 4, not restarted
    assert rec2.saved["optimizer"]["param_groups"][0]["lr"] == pytest.approx(0.0, abs=1e-9)  # cosine ended at zero after 8 steps
    assert mid["optimizer"]["param_groups"][0]["lr"] == pytest.approx(LR * lr_factor(LrSchedule.COSINE, 0.0, 8)(4))


def test_a_resume_after_an_early_stop_does_not_train_again():
    rec = Recording()
    loop.use(rec)
    s = spec(early_stopping_patience=1)
    fit(kit([0.9, 0.8]), s)  # stops after epoch 2
    assert rec.saved["epoch"] == 2
    loop.use(Recording(rec.saved))
    done = fit(kit([0.8]), s)  # no epoch left: the best one is restored and scored from its checkpoint, not evaluated again
    assert len(done.history) == 2 and done.metrics == {"top1": 0.9}


def test_a_non_root_rank_stops_when_the_root_does():
    class Follower(Session):
        rank, world_size = 1, 2

        def agree(self, stop):
            return True  # what the root's broadcast delivers

    loop.use(Follower())
    k = kit([])
    steps = []
    inner = k.loss
    k.loss = lambda m, b, d: (steps.append(1), inner(m, b, d))[1]
    assert fit(k, spec(epochs=5, early_stopping_patience=2)) is None
    assert len(steps) == 2  # one epoch of two batches, then the root's verdict ended the loop


def test_spec_parses_the_recipe_and_old_specs_default_to_the_old_behaviour():
    rows = classification_rows()[0]
    doc = spec_doc("image-classification", "m", rows)
    old = parse_spec(json.dumps(doc))
    assert (old.lr_schedule, old.warmup_fraction, old.augmentation, old.early_stopping_patience) == (LrSchedule.CONSTANT, 0.0, Augmentation.NONE, 0)
    doc["payload"].update(lr_schedule="cosine", warmup_fraction=0.1, augmentation="flip_crop", early_stopping_patience=3)
    new = parse_spec(json.dumps(doc))
    assert (new.lr_schedule, new.warmup_fraction, new.augmentation, new.early_stopping_patience) == (LrSchedule.COSINE, 0.1, Augmentation.FLIP_CROP, 3)
    assert math.isclose(new.warmup_fraction, 0.1)
