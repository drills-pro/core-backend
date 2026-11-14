"""The timer wiring: which pollers every replica runs (each claims its own rows) and which elect a leader."""
import asyncio

from app import background


def test_the_claiming_pollers_are_real_pollers():
    names = {name for name, _, _ in background.POLLERS}
    assert background.CLAIMING <= names and len(names) == len(background.POLLERS)


def test_the_ray_following_reconcilers_are_the_claiming_ones_and_the_cheap_sweeps_elect_a_leader():
    assert background.CLAIMING == {"training-reconcile", "import-reconcile"}
    assert {"import-expiry", "dataset-purge", "rate-limit-purge"}.isdisjoint(background.CLAIMING)


def _run_one_tick(monkeypatch, name):
    calls = []

    async def poll():
        calls.append("polled")

    async def leader(n, p):
        calls.append("leader")

    async def stop_after_one(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(background, "_run_as_leader", leader)
    monkeypatch.setattr(background.asyncio, "sleep", stop_after_one)
    try:
        asyncio.run(background._run_forever(name, poll, 1))
    except asyncio.CancelledError:
        pass
    return calls


def test_a_claiming_poller_runs_on_every_replica_without_the_leader_lock(monkeypatch):
    assert _run_one_tick(monkeypatch, "training-reconcile") == ["polled"]


def test_the_other_pollers_run_under_the_leader_lock(monkeypatch):
    assert _run_one_tick(monkeypatch, "dataset-purge") == ["leader"]
