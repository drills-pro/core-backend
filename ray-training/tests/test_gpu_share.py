"""cap_gpu_share: a fractional GPU job caps its own memory at its share."""
import pytest
import torch

from ray_training.result import cap_gpu_share


@pytest.mark.parametrize(("available", "gpu", "capped"), [(True, 0.25, True), (True, 1, False), (True, 0, False), (True, 2, False), (False, 0.25, False)])
def test_only_a_fraction_on_a_cuda_machine_is_capped(monkeypatch, available, gpu, capped):
    calls = []
    monkeypatch.setattr(torch.cuda, "is_available", lambda: available)
    monkeypatch.setattr(torch.cuda, "set_per_process_memory_fraction", lambda f: calls.append(f))
    assert cap_gpu_share(gpu) is capped
    assert calls == ([gpu] if capped else [])
