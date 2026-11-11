#!/bin/sh
# Local stacks have no GPU, and every seeded preset asks for one (so a run would wait forever). Make the `small`
# image-classification preset a CPU preset in the compose database only; the cluster's presets are not touched.
# Idempotent. Presets are data (task_presets), so this is the same edit /v1/admin/training-presets makes.
set -eu

docker compose exec -T postgres psql -U postgres -d core -v ON_ERROR_STOP=1 -q <<'SQL'
UPDATE task_presets
   SET gpu = 0, cpu = 2, memory_bytes = 6442450944, max_runtime_s = 1800,
       hyperparameters = jsonb_set(hyperparameters, '{epochs,default}', '3'), updated_by = 'local-bootstrap'
 WHERE task = 'image-classification' AND name = 'small';
SQL
echo "preset image-classification/small: CPU only (gpu 0, cpu 2, 6 GiB), 3 epochs"
