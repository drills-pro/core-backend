#!/bin/sh
# One tabular CSV import through the whole local stack: create project and dataset, upload to MinIO, start the Ray
# ingest job, wait for review, commit to lakeFS. Needs `docker compose up -d`, ./scripts/bootstrap.sh and core-api
# running (APP_ENV=local uv run uvicorn app.main:app --port 8090). Uses the dev headers, no token.
#
#   ./scripts/e2e-import.sh            # TENANT and API override the defaults below
set -eu
API="${API:-http://127.0.0.1:8090}"
TENANT="${TENANT:-22222222-2222-2222-2222-222222222222}"  # a demo tenant; each tenant has a cap on active imports
CSV_ROWS=12                                               # the ingest job refuses fewer than dataset_limits.csv_min_rows
POLLS=20 POLL_SECONDS=3

c() { curl -s "$@" -H "X-Tenant-Id: $TENANT" -H "X-User-Id: dev-user"; }
j() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }
JSON="Content-Type: application/json"
csv=$(mktemp); trap 'rm -f "$csv"' EXIT

P=$(c -X POST "$API/v1/projects?name=e2e-import&task_type=tabular-regression" | j "['id']")
DS=$(c -X POST "$API/v1/projects/$P/datasets" -H "$JSON" -d '{"name":"e2e"}' | j "['id']")
IMP=$(c -X POST "$API/v1/projects/$P/datasets/$DS/imports" -H "$JSON" -H "Idempotency-Key: e2e-$(date +%s)" \
  -d '{"source":"upload","params":{"target":"y"}}')
IID=$(echo "$IMP" | j "['id']"); URL=$(echo "$IMP" | j "['upload_url']")
echo "project $P dataset $DS import $IID"

python3 -c "print('x1,x2,y'); [print(f'{i},{i+1},{2*i+3}') for i in range(1, $CSV_ROWS + 1)]" > "$csv"
echo "upload: $(curl -s -o /dev/null -w '%{http_code}' -X PUT --upload-file "$csv" "$URL")"
echo "start: $(c -X POST "$API/v1/projects/$P/datasets/$DS/imports/$IID/start" -o /dev/null -w '%{http_code}')"

IMPORT="$API/v1/projects/$P/datasets/$DS/imports/$IID"
S=
for _ in $(seq 1 $POLLS); do
  S=$(c "$IMPORT" | j "['status']"); echo "status: $S"
  case $S in awaiting_review|failed) break ;; esac
  sleep $POLL_SECONDS
done
c "$IMPORT" | j "['report']"
[ "$S" = awaiting_review ] || exit 1
V=$(c -X POST "$IMPORT/commit")
echo "committed version $(echo "$V" | j "['number']")"
# The committed data must outlive staging: read it back through lakeFS, not from the staged copy.
rows=$(c "$API/v1/projects/$P/datasets/$DS/versions/$(echo "$V" | j "['id']")/preview" | j "['rows'].__len__()")
echo "preview rows: $rows"
[ "$rows" -gt 0 ]
