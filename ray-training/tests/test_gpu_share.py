"""cap_gpu_share: a fractional GPU job caps its own memory at its share."""
import pytest
import torch

from ray_core.result import GIB, cap_gpu_share


@pytest.mark.parametrize(("available", "gpu", "capped"), [(True, 0.25, True), (True, 1, False), (True, 0, False), (True, 2, False), (False, 0.25, False)])
def test_only_a_fraction_on_a_cuda_machine_is_capped(monkeypatch, available, gpu, capped):
    calls = []
    monkeypatch.setattr(torch.cuda, "is_available", lambda: available)
    monkeypatch.setattr(torch.cuda, "set_per_process_memory_fraction", lambda f: calls.append(f))
    assert cap_gpu_share(gpu) is capped
    assert calls == ([gpu] if capped else [])


@pytest.mark.parametrize(("gpu", "vram_gb", "fraction"), [(1, 8, 0.5), (0.5, 8, 0.5), (0.5, 2, 0.125), (1, 16, None), (1, 32, None), (0, 8, None)])
def test_a_declared_vram_caps_the_job_at_the_smaller_of_it_and_its_share(monkeypatch, gpu, vram_gb, fraction):
    calls = []
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "get_device_properties", lambda i: type("P", (), {"total_memory": 16 * GIB})())
    monkeypatch.setattr(torch.cuda, "set_per_process_memory_fraction", lambda f: calls.append(f))
    assert cap_gpu_share(gpu, vram_gb) is (fraction is not None)
    assert calls == ([fraction] if fraction else [])
