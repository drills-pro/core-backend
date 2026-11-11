"""drills.core_backend.predict.*: the legacy POST /v1/predict/{deployment_id} route kept in shadow until it is removed (planning/24, Migrating the old endpoint)."""
from app.core.metrics.base import counter

legacy_calls = counter("predict.legacy_calls", "Calls to the deprecated per-deployment predict route that passed its key check, by deployment_id; zero for 14 days means it can go")
