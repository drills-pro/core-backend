"""An in-memory `predictions` table behind a FakeConn: it answers the SQL of app/modules/user/deployments/prediction_store.py (and the
worker's) with the same rules the real statements have (the Idempotency-Key unique index, the attempt and status fence of every
write-back), so the unit tests run the store's own Python. tests/db runs the SQL itself on a real Postgres."""
import uuid
from datetime import datetime, timedelta, timezone

from app.modules.user.deployments import prediction_store as store
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
            tenant, project, key_id, deployment, model, revision, mode, status, inp, meta, key, hashed, attempt, max_attempts, lease, retention = a
            if key is not None and self._keyed(project, key) is not None:
                return None
            row = dict(
                id=uuid.uuid4(), tenant_id=tenant, project_id=project, api_key_id=key_id, deployment_id=deployment, model_id=model,
                model_revision=revision, mode=mode, status=status, cancel_requested=False, input=inp, output=None, error=None, metadata=meta,
                latency_ms=None, idempotency_key=key, request_hash=hashed, attempt=attempt, max_attempts=max_attempts, lease_owner=None,
                lease_expires_at=None if lease is None else now() + timedelta(seconds=lease), next_attempt_at=now(), created_at=now(),
                started_at=now() if attempt else None, finished_at=None, expires_at=None if retention is None else now() + timedelta(seconds=retention),
                purged_at=None)
            self.preds[row["id"]] = row
            return row
        if sql == store._BY_KEY:
            return self._keyed(*a)
        if sql == store._BY_ID:
            return self.preds.get(a[0])
        if sql == store._FINISH:
            prediction_id, attempt, status, output, error, latency, retention, release, expected = a
            row = self.preds[prediction_id]
            if row["attempt"] != attempt or row["status"] != expected:
                return None
            row.update(status=status, output=output, error=error, latency_ms=latency, finished_at=now(), expires_at=now() + timedelta(seconds=retention), lease_expires_at=None)
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

