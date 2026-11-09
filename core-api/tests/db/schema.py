"""What the database tests know about the schema: the runtime role and one seed row per tenant table.

`$1` is the tenant id. A child row selects its parent's id from the same tenant, so parents come first. A new tenant
table adds a line to SEEDS; test_every_tenant_table_is_seeded fails until it does.
"""
from app.core.authz import InviteRole, Role, Scope, TenantRole
from app.core.enums import ImportSource
from app.modules.user.deployments.constants import PredictionMode, PredictionStatus
from app.modules.user.training.constants import EventType
from app.modules.user.usage.constants import UsageResource, UsageUnit

APP_ROLE = "core_app"  # the runtime role (compose/postgres/core-api.sql): RLS binds it, not its owner backend_admin
TENANT_SEED = "INSERT INTO tenants (id, name) VALUES ($1, 'tenant')"  # tenants has no RLS: it is the root the rest hangs on
SEEDS = (
    ("teams", "INSERT INTO teams (tenant_id, name) VALUES ($1, 'team')"),
    ("projects", "INSERT INTO projects (tenant_id, name) VALUES ($1, 'project')"),
    ("project_members",
     f"INSERT INTO project_members (tenant_id, project_id, user_sub, role) "
     f"SELECT tenant_id, id, 'u1', '{Role.VIEWER}' FROM projects WHERE tenant_id = $1"),
    ("audit_log", "INSERT INTO audit_log (tenant_id, actor, action, entity_type, entity_id) VALUES ($1, 'u1', 'seeded', 'tenant', 'x')"),
    ("usage_events",
     f"INSERT INTO usage_events (tenant_id, project_id, resource_type, quantity, unit, occurred_at, source_ref, pricing_version) "
     f"SELECT tenant_id, id, '{UsageResource.TRAINING}', 1, '{UsageUnit.GPU_SECONDS}', now(), tenant_id::text, 'v1' "
     f"FROM projects WHERE tenant_id = $1"),
    ("datasets", "INSERT INTO datasets (tenant_id, project_id, name) SELECT tenant_id, id, 'dataset' FROM projects WHERE tenant_id = $1"),
    ("dataset_imports",
     f"INSERT INTO dataset_imports (tenant_id, project_id, dataset_id, source, idempotency_key) "
     f"SELECT tenant_id, project_id, id, '{ImportSource.UPLOAD}', 'k' FROM datasets WHERE tenant_id = $1"),
    ("dataset_versions",
     "INSERT INTO dataset_versions (tenant_id, dataset_id, number, lakefs_commit, import_id) "
     "SELECT tenant_id, dataset_id, 1, 'c', id FROM dataset_imports WHERE tenant_id = $1"),
    ("training_runs",
     "INSERT INTO training_runs (tenant_id, project_id, idempotency_key, ray_job_id, dataset_ref) "
     "SELECT tenant_id, id, 'k', 'r', 'x' FROM projects WHERE tenant_id = $1"),
    ("training_run_events",
     f"INSERT INTO training_run_events (run_id, seq, tenant_id, type, epoch, epochs, metrics) "
     f"SELECT id, 0, tenant_id, '{EventType.PROGRESS}', 1, 2, '{{}}' FROM training_runs WHERE tenant_id = $1"),
    ("deployments",
     "INSERT INTO deployments (tenant_id, project_id, training_run_id, route_prefix) "
     "SELECT tenant_id, project_id, id, 'p-' || tenant_id::text FROM training_runs WHERE tenant_id = $1"),
    ("models", "INSERT INTO models (tenant_id, project_id, name) SELECT tenant_id, id, 'm' FROM projects WHERE tenant_id = $1"),
    ("model_versions",
     "INSERT INTO model_versions (tenant_id, project_id, training_run_id, mlflow_model_name, mlflow_version, model_id) "
     "SELECT r.tenant_id, r.project_id, r.id, 'm', '1', m.id FROM training_runs r JOIN models m ON m.project_id = r.project_id "
     "WHERE r.tenant_id = $1"),
    ("deployment_test_calls",
     "INSERT INTO deployment_test_calls (tenant_id, deployment_id, request) "
     "SELECT tenant_id, id, '{}'::jsonb FROM deployments WHERE tenant_id = $1"),
    ("predict_calls",
     "INSERT INTO predict_calls (tenant_id, deployment_id, training_run_id, status, latency_ms, request_bytes) "
     "SELECT tenant_id, id, training_run_id, 200, 1, 1 FROM deployments WHERE tenant_id = $1"),
    ("tenant_members", f"INSERT INTO tenant_members (tenant_id, user_sub, role) VALUES ($1, 'u1', '{TenantRole.MEMBER}')"),
    ("tenant_invitations",
     f"INSERT INTO tenant_invitations (tenant_id, email, role, token_hash, invited_by, expires_at) "
     f"VALUES ($1, 'u2@example.com', '{InviteRole.MEMBER}', gen_random_uuid()::text, 'u1', now() + interval '1 day')"),
    ("ownership_offers",
     "INSERT INTO ownership_offers (tenant_id, from_sub, to_sub, expires_at) VALUES ($1, 'u1', 'u2', now() + interval '1 day')"),
    ("api_keys",
     "INSERT INTO api_keys (tenant_id, project_id, name, prefix, key_hash, created_by) "
     "SELECT tenant_id, id, 'key', 'pre', tenant_id::text, 'u1' FROM projects WHERE tenant_id = $1"),
    ("api_key_scopes",
     f"INSERT INTO api_key_scopes (key_id, tenant_id, scope) SELECT id, tenant_id, '{Scope.PREDICT}' FROM api_keys WHERE tenant_id = $1"),
    ("api_key_models",
     "INSERT INTO api_key_models (key_id, tenant_id, model_id) "
     "SELECT k.id, k.tenant_id, m.id FROM api_keys k JOIN models m ON m.project_id = k.project_id WHERE k.tenant_id = $1"),
    ("predictions",
     f"INSERT INTO predictions (tenant_id, project_id, api_key_id, deployment_id, model_id, model_revision, mode, status, max_attempts) "
     f"SELECT d.tenant_id, d.project_id, k.id, d.id, m.id, '1', '{PredictionMode.BACKGROUND}', '{PredictionStatus.QUEUED}', 3 "
     f"FROM deployments d JOIN api_keys k ON k.project_id = d.project_id JOIN models m ON m.project_id = d.project_id WHERE d.tenant_id = $1"),
    ("prediction_events",
     f"INSERT INTO prediction_events (prediction_id, seq, tenant_id, status) "
     f"SELECT id, 1, tenant_id, '{PredictionStatus.QUEUED}' FROM predictions WHERE tenant_id = $1"),
)
