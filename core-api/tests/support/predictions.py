"""An in-memory `predictions` table behind a FakeConn: it answers the SQL of app/modules/user/deployments/prediction_store.py (and the
worker's) with the same rules the real statements have (the Idempotency-Key unique index, the attempt and status fence of every
write-back), so the unit tests run the store's own Python. tests/db runs the SQL itself on a real Postgres."""
import uuid
from datetime import datetime, timedelta, timezone

from app.modules.user.deployments import prediction_service, prediction_store as store
from app.modules.user.deployments.constants import PredictionMode, PredictionStatus
from tests.support import FakeConn


def now() -> datetime:
    return datetime.now(timezone.utc)


class PredictionsConn(FakeConn):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.preds: dict[uuid.UUID, dict] = {}
        self.events: list[tuple] = []  # (prediction_id, status, data), in write order

    def _keyed(self, project, key):
        return next((r for r in self.preds.values() if r["project_id"] == project and r["idempotency_key"] == key), None)

    async def fetchrow(self, sql, *a):
        if sql == store._INSERT:
            tenant, project, key_id, deployment, model, revision, mode, status, inp, meta, key, hashed, attempt, max_attempts, lease, retention, new_id, input_ref, source = a
            if key is not None and self._keyed(project, key) is not None:
                return None
            row = dict(
                id=new_id or uuid.uuid4(), input_ref=input_ref, output_ref=None, tenant_id=tenant, project_id=project, api_key_id=key_id, deployment_id=deployment, model_id=model,
                model_revision=revision, mode=mode, status=status, cancel_requested=False, input=inp, output=None, error=None, metadata=meta,
                latency_ms=None, idempotency_key=key, request_hash=hashed, attempt=attempt, max_attempts=max_attempts, lease_owner=None,
                lease_expires_at=None if lease is None else now() + timedelta(seconds=lease), next_attempt_at=now(), created_at=now(),
                started_at=now() if attempt else None, finished_at=None, expires_at=None if retention is None else now() + timedelta(seconds=retention),
                purged_at=None, source=source)
            self.preds[row["id"]] = row
            return row
        if sql == store._BY_KEY:
            return self._keyed(*a)
        if sql == store._BY_ID:
            return self.preds.get(a[0])
        if sql in (prediction_service._OWNED, prediction_service._OWNED_LOCKED):
            row = self.preds.get(a[0])
            return row if row is not None and row["api_key_id"] == a[1] and row["project_id"] == a[2] else None
        if sql == store._CANCEL_QUEUED:
            prediction_id, status, retention, expected = a
            row = self.preds[prediction_id]
            if row["status"] != expected:
                return None
            row.update(status=status, finished_at=now(), expires_at=now() + timedelta(seconds=retention), lease_owner=None, lease_expires_at=None)
            return row
        if sql == store._REQUEST_CANCEL:
            prediction_id, mode, expected = a
            row = self.preds[prediction_id]
            if row["mode"] != mode or row["status"] != expected:
                return None
            row["cancel_requested"] = True
            return row
        if sql == store._REQUEUE:
            prediction_id, attempt, status, delay, expected = a
            row = self.preds[prediction_id]
            if row["attempt"] != attempt or row["status"] != expected:
                return None
            row.update(status=status, lease_owner=None, lease_expires_at=None, next_attempt_at=now() + timedelta(seconds=delay))
            return row
        if sql == store._FINISH:
            prediction_id, attempt, status, output, error, latency, retention, release, expected, output_ref = a
            row = self.preds[prediction_id]
            if row["attempt"] != attempt or row["status"] != expected:
                return None
            row.update(status=status, output=output, output_ref=output_ref, error=error, latency_ms=latency, finished_at=now(), expires_at=now() + timedelta(seconds=retention), lease_expires_at=None)
            if release:
                row["idempotency_key"] = None
            return row
        if sql == store._ABANDON:
            prediction_id, status, error, retention, mode, expected = a
            row = self.preds[prediction_id]
            if row["mode"] != mode or row["status"] != expected or row["lease_expires_at"] is None or row["lease_expires_at"] >= now():
                return None
            row.update(status=status, error=error, finished_at=now(), expires_at=now() + timedelta(seconds=retention), lease_expires_at=None)
            return row
        return await super().fetchrow(sql, *a)

    async def fetchval(self, sql, *a):
        if sql in (store._FLAG, store._FLAG_LOCKED):
            row = self.preds.get(a[0])
            return row["cancel_requested"] if row is not None and row["attempt"] == a[1] and row["status"] == a[2] else None
        if sql == store._STATUS:
            return self.preds[a[0]]["status"] if a[0] in self.preds else None
        return await super().fetchval(sql, *a)

    async def fetch(self, sql, *a):
        if sql == store._EVENTS_AFTER:
            prediction_id, after = a
            own = [e for e in self.events if e[0] == prediction_id]
            return [{"seq": i, "status": status, "data": data} for i, (_, status, data) in enumerate(own, start=1) if i > after]
        if sql == store._CLAIM:
            tenant, mode, status, limit, new_status, owner, lease = a
            due = [r for r in self.preds.values() if str(r["tenant_id"]) == str(tenant) and r["mode"] == mode and r["status"] == status and r["next_attempt_at"] <= now()]
            picked = sorted(due, key=lambda r: r["created_at"])[:limit]
            for row in picked:
                row.update(status=new_status, attempt=row["attempt"] + 1, lease_owner=owner, lease_expires_at=now() + timedelta(seconds=lease), started_at=row["started_at"] or now())
            return picked
        if sql == store._EXPIRED:
            tenant, mode, status = a
            return [r for r in self.preds.values() if str(r["tenant_id"]) == str(tenant) and r["mode"] == mode and r["status"] == status and r["lease_expires_at"] < now()]
        return await super().fetch(sql, *a)

    def tenants_with_work(self) -> list[tuple]:
        return [(t,) for t in {r["tenant_id"] for r in self.preds.values() if r["mode"] == PredictionMode.BACKGROUND and r["status"] in (PredictionStatus.QUEUED, PredictionStatus.IN_PROGRESS)}]

    async def execute(self, sql, *a):
        if sql == store._EVENT:
            prediction_id, _, status, data = a
            self.events.append((prediction_id, status, data))
            return None
        return await super().execute(sql, *a)

    def statuses(self, prediction_id) -> list[str]:
        return [s for p, s, _ in self.events if p == prediction_id]

    def expire_lease(self, prediction_id) -> None:
        self.preds[prediction_id]["lease_expires_at"] = now() - timedelta(seconds=1)

