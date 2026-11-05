#!/bin/sh
# The training and deployment API through the local stack, without a trainer: no GPU or torch image runs here, so
# two runs are seeded in Postgres (a succeeded detection run with class names, a running run with an events token)
# and everything else goes through the real API: presets, events + progress, deploy (C-01, C-13), stop, start,
# delete (C-02), the deployment quota, and a NaN body. Not covered: a real training run, a real model served.
# Needs `docker compose up -d`, ./scripts/bootstrap.sh and core-api running (see e2e-import.sh).
#
#   COMPOSE_PROJECT_NAME=core-backend ./scripts/e2e-train-deploy.sh
set -eu
cd "$(dirname "$0")/.."
API="${API:-http://127.0.0.1:8090}"
TENANT="${TENANT:-22222222-2222-2222-2222-222222222222}"  # the demo tenant, which has a team
RUN_ID=$(python3 -c "import uuid;print(uuid.uuid4())")
TOKEN=e2e-$(python3 -c "import secrets;print(secrets.token_urlsafe(16))")
fails=0

c() { curl -s -o /dev/null -w '%{http_code}' "$@" -H "X-Tenant-Id: $TENANT" -H "X-User-Id: dev-user"; }
body() { curl -s "$@" -H "X-Tenant-Id: $TENANT" -H "X-User-Id: dev-user"; }
j() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }
sql() { docker compose exec -T postgres psql -U backend_admin -d core -At -v ON_ERROR_STOP=1 -c "$1"; }
check() { # check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "ok    $1"; else echo "FAIL  $1: expected $2, got $3"; fails=$((fails + 1)); fi
}
JSON="Content-Type: application/json"

# Earlier runs of this script left live deployments that count against the plan limit: end them.
sql "UPDATE deployments SET status = 'deleted', predict_key_hash = NULL
     WHERE status <> 'deleted' AND project_id IN (SELECT id FROM projects WHERE name = 'e2e-train-deploy')" >/dev/null

P=$(body -X POST "$API/v1/projects?name=e2e-train-deploy&task_type=object-detection" | j "['id']")
echo "project $P"

# --- presets (C-25): the form lists the recipe fields
check "presets expose the recipe" "True" "$(body "$API/v1/projects/$P/training-presets" | python3 -c "import sys,json;d=json.load(sys.stdin);print(len(d)>0 and all(k in d[0] for k in ('lr_schedule','augmentation','early_stopping_patience')))")"

# --- seed two runs: one done with a model (detection), one running with an events channel
HASH=$(python3 -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$TOKEN")
sql "INSERT INTO training_runs (id, tenant_id, project_id, idempotency_key, ray_job_id, dataset_ref, status, started_at, events_token_hash)
     VALUES ('$RUN_ID', '$TENANT', '$P', 'e2e-$RUN_ID', 'e2e-job', 'e2e', 'running', now(), '$HASH')" >/dev/null
DONE_RUNS=""
for i in 1 2 3 4; do
  R=$(python3 -c "import uuid;print(uuid.uuid4())")
  sql "INSERT INTO training_runs (id, tenant_id, project_id, idempotency_key, ray_job_id, dataset_ref, status, mlflow_run_id, report, completed_at)
       VALUES ('$R', '$TENANT', '$P', 'e2e-$R', 'e2e-job-$i', 'e2e', 'succeeded', 'mlflow-$R', '{\"classes\": [\"cat\", \"dog\"]}', now())" >/dev/null
  DONE_RUNS="$DONE_RUNS $R"
done
set -- $DONE_RUNS; D1=$1; D2=$2; D3=$3; D4=$4

# --- progress events (C-07)
EV="$API/internal/v1/training-runs/$RUN_ID/events"
ev() { curl -s -o /dev/null -w '%{http_code}' -X POST "$EV" -H "$JSON" -H "Authorization: Bearer $1" -d "$2"; }
check "event with the wrong token is 401" 401 "$(ev nope '{"type":"progress","seq":0,"epoch":1,"epochs":3,"metrics":{"loss":0.9}}')"
check "event is stored (201)" 201 "$(ev "$TOKEN" '{"type":"progress","seq":0,"epoch":1,"epochs":3,"metrics":{"loss":0.9}}')"
check "same seq again is a no-op (200)" 200 "$(ev "$TOKEN" '{"type":"progress","seq":0,"epoch":1,"epochs":3,"metrics":{"loss":0.9}}')"
check "second epoch is stored" 201 "$(ev "$TOKEN" '{"type":"progress","seq":1,"epoch":2,"epochs":3,"metrics":{"loss":0.5}}')"
check "GET run shows the latest epoch" 2 "$(body "$API/v1/projects/$P/training-runs/$RUN_ID" | j "['progress']['latest']['epoch']")"
check "GET run shows the history" 2 "$(body "$API/v1/projects/$P/training-runs/$RUN_ID" | j "['progress']['history'].__len__()")"
check "NaN in a body is a 422, not a 500" 422 "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$EV" -H "$JSON" -H "Authorization: Bearer $TOKEN" -d '{"type":"progress","seq":2,"epoch":3,"epochs":3,"metrics":{"loss":NaN}}')"
sql "UPDATE training_runs SET status = 'succeeded' WHERE id = '$RUN_ID'" >/dev/null
check "event after the run ended is 409" 409 "$(ev "$TOKEN" '{"type":"progress","seq":2,"epoch":3,"epochs":3,"metrics":{"loss":0.2}}')"
check "progress is kept after the run ended" 2 "$(body "$API/v1/projects/$P/training-runs/$RUN_ID" | j "['progress']['latest']['epoch']")"

# --- deploy (C-01 detection run, C-13 idempotency + one live deployment per run)
DEP="$API/v1/projects/$P/deployments"
check "deploy needs an Idempotency-Key" 422 "$(c -X POST "$DEP?training_run_id=$D1")"
OUT=$(body -X POST "$DEP?training_run_id=$D1" -H "Idempotency-Key: k-$D1")
DEP1=$(echo "$OUT" | j "['id']")
check "a detection run deploys (202, key shown once)" True "$(echo "$OUT" | python3 -c "import sys,json;print('predict_key' in json.load(sys.stdin))")"
KEY1=$(echo "$OUT" | j "['predict_key']")
check "same key + same run replays the deployment" "$DEP1" "$(body -X POST "$DEP?training_run_id=$D1" -H "Idempotency-Key: k-$D1" | j "['id']")"
check "same key + other run is 409" 409 "$(c -X POST "$DEP?training_run_id=$D2" -H "Idempotency-Key: k-$D1")"
check "second deploy of the same run is 409" 409 "$(c -X POST "$DEP?training_run_id=$D1" -H "Idempotency-Key: other-$D1")"

# --- lifecycle (C-02): a live shared deployment is seeded so no Ray Serve is needed
for R in $D2 $D3; do  # with D1's deployment that is 3 live, the free plan's limit
  sql "INSERT INTO deployments (tenant_id, project_id, training_run_id, route_prefix, status, serving, predict_key_hash)
       VALUES ('$TENANT', '$P', '$R', '/predict/$R', 'active', 'shared', 'x')" >/dev/null
done
check "deploy over the plan limit is 429" 429 "$(c -X POST "$DEP?training_run_id=$D4" -H "Idempotency-Key: quota-$D4")"
SID=$(sql "SELECT id FROM deployments WHERE training_run_id = '$D2'")
check "stop an active deployment" stopped "$(body -X POST "$DEP/$SID/stop" | j "['status']")"
check "stop again is idempotent" stopped "$(body -X POST "$DEP/$SID/stop" | j "['status']")"
check "try-it on a stopped deployment is 409" 409 "$(c -X POST "$DEP/$SID/test" -H "$JSON" -d '{}')"
check "start brings it back" active "$(body -X POST "$DEP/$SID/start" | j "['status']")"
check "delete is 204" 204 "$(c -X DELETE "$DEP/$SID")"
check "a deleted deployment is gone (404)" 404 "$(c "$DEP/$SID")"
check "a deleted deployment is not listed" False "$(body "$DEP" | python3 -c "import sys,json;print(any(d['id']=='$SID' for d in json.load(sys.stdin)))")"
check "delete freed the quota slot (deploy is 202 again)" 202 "$(c -X POST "$DEP?training_run_id=$D4" -H "Idempotency-Key: quota2-$D4")"

# --- try-it history is bounded (C-16)
check "test-calls limit over the max is 422" 422 "$(c "$DEP/$DEP1/test-calls?limit=100000")"

echo
if [ "$fails" -eq 0 ]; then echo "all checks passed"; else echo "$fails check(s) failed"; exit 1; fi
