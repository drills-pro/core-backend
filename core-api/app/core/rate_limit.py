"""req/04 FR4: rate limiting on the inference endpoint per deployment.

In-process sliding window — correct for a single core-api instance (this
stack's current shape); a multi-instance deployment would need this in
Redis/Postgres instead, but the enforcement point and behavior are the
same either way.
"""
import time
from collections import defaultdict, deque

_windows: dict[str, deque] = defaultdict(deque)


def check(key: str, limit_per_minute: int) -> bool:
    """Returns True if the request is allowed, False if it should be 429'd."""
    now = time.monotonic()
    window = _windows[key]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= limit_per_minute:
        return False
    window.append(now)
    return True
