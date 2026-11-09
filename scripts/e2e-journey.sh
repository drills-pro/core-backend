#!/bin/sh
# The whole product path through the local stack, no seeding: tiny image-classification project -> zip upload ->
# ingest -> commit -> training run -> deployment -> predict with the one-time key -> delete -> the key no longer works.
# Needs `docker compose up -d`, ./scripts/bootstrap.sh, core-api running (see e2e-import.sh), a Ray worker that can
# train and a predictor that can serve (a real run: minutes on CPU). Uses the dev headers, no token.
# (e2e-train-deploy.sh is the fast variant that seeds its runs in Postgres instead.)
#
#   ./scripts/e2e-journey.sh           # API, TENANT, POLLS, POLL_SECONDS override the defaults below
set -eu
API="${API:-http://127.0.0.1:8090}"
TENANT="${TENANT:-22222222-2222-2222-2222-222222222222}"  # a demo tenant with a team
IMPORT_POLLS=${IMPORT_POLLS:-20} POLL_SECONDS=${POLL_SECONDS:-3}
POLLS=${POLLS:-240}                                       # training polls, POLL_SECONDS each (a CPU run is slow)
DEPLOY_POLLS=${DEPLOY_POLLS:-40}
JSON="Content-Type: application/json"

fail() { echo "FAIL: $*" >&2; exit 1; }
RESP=$(mktemp); ZIP=$(mktemp); trap 'rm -f "$RESP" "$ZIP"' EXIT
# call <expected status> <curl args...>: the body lands in $RESP, any other status stops the script with it.
call() {
  want=$1; shift
  got=$(curl -s -o "$RESP" -w '%{http_code}' "$@" -H "X-Tenant-Id: $TENANT" -H "X-User-Id: dev-user")
  [ "$got" = "$want" ] || fail "$* -> HTTP $got, expected $want: $(cat "$RESP")"
}
j() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)" < "$RESP"; }

# Two classes of 64x64 PNGs (red-ish vs blue-ish, written with zlib/struct), zipped as <class>/<n>.png.
python3 - "$ZIP" <<'PY'
import random, struct, sys, zipfile, zlib
def png(rgb, size=64):
    row = lambda: b"\0" + b"".join(bytes(min(255, max(0, c + random.randint(-20, 20))) for c in rgb) for _ in range(size))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(row() for _ in range(size)))) + chunk(b"IEND", b""))
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_STORED) as z:  # stored: keeps the zip-bomb ratio check quiet
    for label, rgb in (("red", (200, 40, 40)), ("blue", (40, 40, 200))):
        for i in range(20):
            z.writestr(f"{label}/{i}.png", png(rgb))
PY
B64=$(python3 -c "
import base64, zipfile, sys
print(base64.b64encode(zipfile.ZipFile(sys.argv[1]).read('red/0.png')).decode())" "$ZIP")

# --- project, dataset, import
call 201 -X POST "$API/v1/projects?name=e2e-journey&task_type=image-classification"; P=$(j "['id']")
call 201 -X POST "$API/v1/projects/$P/datasets" -H "$JSON" -d '{"name":"e2e"}'; DS=$(j "['id']")
call 201 -X POST "$API/v1/projects/$P/datasets/$DS/imports" -H "$JSON" -H "Idempotency-Key: e2e-$(date +%s)" -d '{"source":"upload"}'
IID=$(j "['id']"); URL=$(j "['upload_url']")
echo "project $P dataset $DS import $IID"
up=$(curl -s -o /dev/null -w '%{http_code}' -X PUT --upload-file "$ZIP" "$URL"); [ "$up" = 200 ] || fail "upload to the presigned URL -> HTTP $up"
IMPORT="$API/v1/projects/$P/datasets/$DS/imports/$IID"
call 202 -X POST "$IMPORT/start"

S=
for _ in $(seq 1 "$IMPORT_POLLS"); do
  call 200 "$IMPORT"; S=$(j "['status']"); echo "import: $S"
  case $S in awaiting_review|failed) break ;; esac
  sleep "$POLL_SECONDS"
done
[ "$S" = awaiting_review ] || fail "import ended as '$S': $(j "['report']")"
call 200 -X POST "$IMPORT/commit"; V=$(j "['id']"); echo "committed version $(j "['number']")"

# --- training run (PRESET, else the first the project offers; a local stack trains only `small`, see steps/cpu-preset.sh)
call 200 "$API/v1/projects/$P/training-presets"; PRESET="${PRESET:-$(j "[0]['name']")}"
call 202 -X POST "$API/v1/projects/$P/training-runs" -H "$JSON" -H "Idempotency-Key: e2e-run-$(date +%s)" \
  -d "{\"dataset_version_id\":\"$V\",\"preset\":\"$PRESET\"}"
RUN=$(j "['id']"); echo "training run $RUN (preset $PRESET)"
S=
for _ in $(seq 1 "$POLLS"); do
  call 200 "$API/v1/projects/$P/training-runs/$RUN"; S=$(j "['status']"); echo "training: $S"
  case $S in succeeded|failed|cancelled) break ;; esac
  sleep "$POLL_SECONDS"
done
[ "$S" = succeeded ] || fail "training ended as '$S': $(j "['report']")"

# The settle step registers the model itself; this covers the case it did not. Deploy does not need it, so only report.
rc=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/v1/projects/$P/models/$RUN/register?name=e2e-model" -H "X-Tenant-Id: $TENANT" -H "X-User-Id: dev-user")
echo "register model: HTTP $rc"

# --- deployment
DEP="$API/v1/projects/$P/deployments"
call 202 -X POST "$DEP?training_run_id=$RUN" -H "Idempotency-Key: e2e-dep-$RUN"
D=$(j "['id']"); KEY=$(j "['predict_key']"); echo "deployment $D"
S=
for _ in $(seq 1 "$DEPLOY_POLLS"); do
  call 200 "$DEP/$D"; S=$(j "['status']"); echo "deployment: $S"
  case $S in active|failed) break ;; esac
  sleep "$POLL_SECONDS"
done
[ "$S" = active ] || fail "deployment ended as '$S', not active"

# --- predict, then delete and predict again
predict() { curl -s -o "$RESP" -w '%{http_code}' -X POST "$API/v1/predict/$D" -H "$JSON" -H "X-Predict-Key: $KEY" -d "{\"image_base64\":\"$B64\"}"; }
[ "$(predict)" = 200 ] || fail "predict -> $(cat "$RESP")"
python3 -c "import sys,json;assert 'predictions' in json.load(sys.stdin)" < "$RESP" || fail "predict answer has no 'predictions': $(cat "$RESP")"
echo "predict: $(cat "$RESP")"
call 204 -X DELETE "$DEP/$D"
# Delete clears the predict key, so the key lookup finds nothing and core-api answers 401; 409 is the stopped/inactive answer.
case $(predict) in 401|409) echo "predict after delete refused" ;; *) fail "predict after delete -> $(cat "$RESP")" ;; esac
echo "journey passed"
