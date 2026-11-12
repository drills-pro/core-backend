"""Delivers the events outbox to webhook endpoints. One leader runs it (app/background.py); each endpoint is sent what
lies after its cursor, in order, and the cursor moves only past what the receiver accepted (2xx), so delivery is at
least once and the event's seq is the receiver's idempotency key. A failing endpoint backs off, then is disabled."""
import asyncio
import json
import logging
import socket
import time

import httpx

from app.integrations.job_lifecycle import fetch
from app.core.tenant_conn import TenantConn
from app.modules.user.job_events import JobEvent
from app.modules.user.webhooks import constants as c
from app.modules.user.webhooks import signing

logger = logging.getLogger(__name__)

_DUE = (
    "SELECT id, project_id, url, last_seq, failures FROM webhook_endpoints "
    "WHERE disabled_at IS NULL AND next_attempt_at <= now() ORDER BY next_attempt_at"
)
_EVENTS = (
    "SELECT seq, kind, entity_id, status, occurred_at FROM events WHERE project_id = $1 AND seq > $2 ORDER BY seq LIMIT $3"
)
# Compare-and-set on the cursor: a second dispatcher (or a retry after a crash) moves it at most once.
_MOVE = (
    "UPDATE webhook_endpoints SET last_seq = $3, failures = 0, next_attempt_at = now() WHERE id = $1 AND last_seq = $2"
)
_FAIL = (
    "UPDATE webhook_endpoints SET failures = failures + 1, next_attempt_at = now() + make_interval(secs => $2), "
    "disabled_at = CASE WHEN failures + 1 >= $3 THEN now() END WHERE id = $1"
)


def backoff(failures: int) -> float:
    """Seconds an endpoint waits after its `failures`-th failed tick in a row."""
    return min(c.BACKOFF_BASE_SECONDS * c.BACKOFF_FACTOR ** (failures - 1), c.BACKOFF_MAX_SECONDS)


async def _public_host(host: str) -> bool:
    """Every address the name resolves to is publicly routable (a name that points inside the cluster is refused)."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(infos) and not any(signing.blocked_address(i[4][0]) for i in infos)


async def deliver(client: httpx.AsyncClient, secret: str, url: str, project_id, event: JobEvent) -> bool:
    body = json.dumps({"project_id": str(project_id), **event.model_dump(mode="json")}).encode()
    now = int(time.time())
    headers = {
        "Content-Type": "application/json", c.TIMESTAMP_HEADER: str(now), c.EVENT_ID_HEADER: str(event.seq),
        c.SIGNATURE_HEADER: signing.sign(secret, now, body),
    }
    try:
        r = await client.post(url, content=body, headers=headers, timeout=c.DELIVERY_TIMEOUT_SECONDS, follow_redirects=False)
    except httpx.HTTPError as e:
        logger.info("webhook %s: %s", url, type(e).__name__)
        return False
    return r.is_success


async def _send_endpoint(client: httpx.AsyncClient, tenant_id: str, ep: dict) -> None:
    host = httpx.URL(ep["url"]).host
    async with TenantConn(tenant_id) as conn:
        rows = await conn.fetch(_EVENTS, ep["project_id"], ep["last_seq"], c.DELIVERY_BATCH)
    if not rows:
        return
    ok, sent = await _public_host(host), ep["last_seq"]
    secret = signing.endpoint_secret(ep["id"]) if ok else ""
    for row in rows if ok else []:
        event = JobEvent.model_validate(dict(row))
        if not await deliver(client, secret, ep["url"], ep["project_id"], event):
            ok = False
            break
        sent = event.seq
    async with TenantConn(tenant_id) as conn:
        if sent > ep["last_seq"]:
            await conn.execute(_MOVE, ep["id"], ep["last_seq"], sent)
        if not ok:
            await conn.execute(_FAIL, ep["id"], backoff(ep["failures"] + 1), c.DISABLE_AFTER_FAILURES)


async def dispatch_webhooks() -> None:
    """One tick: every due endpoint of every tenant with something to send."""
    tenants = [str(r[0]) for r in await fetch("SELECT webhook_tenants_due()")]
    if not tenants:
        return
    try:
        signing.master_key()
    except signing.SigningUnavailable:
        logger.warning("webhook endpoints are due but the webhooks secret file is missing")
        return
    async with httpx.AsyncClient() as client:
        for tenant_id in tenants:
            async with TenantConn(tenant_id) as conn:
                endpoints = [dict(r) for r in await conn.fetch(_DUE)]
            for ep in endpoints:
                try:
                    await _send_endpoint(client, tenant_id, ep)
                except Exception:  # noqa: BLE001 - one endpoint never stops the rest
                    logger.exception("webhook endpoint %s", ep["id"])
