"""Worker mode runs the loops; the API runs them only while background.pollers_in_api is on."""
import asyncio
import os
import signal

from app import background, main, worker
from app.core.settings import settings


def _recording(monkeypatch):
    started = []
    monkeypatch.setattr(background, "start", lambda: started.append("start") or [])
    return started


def test_api_lifespan_skips_the_loops_when_the_worker_has_them(monkeypatch):
    started = _recording(monkeypatch)
    monkeypatch.setattr(settings.background, "pollers_in_api", False)

    async def go():
        async with main.lifespan(None):
            pass

    asyncio.run(go())
    assert started == []
    monkeypatch.setattr(settings.background, "pollers_in_api", True)
    asyncio.run(go())
    assert started == ["start"]


def test_worker_starts_the_loops_and_stops_them_on_sigterm(monkeypatch):
    started, stopped = _recording(monkeypatch), []

    async def stop(tasks):
        stopped.append(tasks)

    monkeypatch.setattr(background, "stop", stop)

    async def go():
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
        await worker.run()

    asyncio.run(go())
    assert started == ["start"] and stopped == [[]]
