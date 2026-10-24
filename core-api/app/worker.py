"""The same image in worker mode: `python -m app.worker` runs the background loops and nothing else.

Composition root, like main.py. Each loop elects its leader through a Postgres advisory lock
(app/background.py), so any number of replicas is safe and one of them does each sweep. Set
`background.pollers_in_api: false` on the API once this is deployed, or both run them (harmless, but wasteful).
"""
import asyncio
import logging
import signal

from app import background

logger = logging.getLogger(__name__)


async def run() -> None:
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)
    tasks = background.start()
    logger.info("worker started: %s", ", ".join(t.get_name() for t in tasks))
    try:
        await stopping.wait()
    finally:
        await background.stop(tasks)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
