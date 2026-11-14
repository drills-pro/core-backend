#!/usr/bin/env bash
# Build a task bundle zip (planning/25-task-bundles.md, "Bundle format"), for CI to stage and register.
#
#   scripts/build-bundle.sh task-bundles/tasks/<slug> [out dir, default dist]
#
# The zip holds manifest.json at its root and the bundle's .py files at their paths (tests/ and __pycache__ left out), so Ray puts
# the package on sys.path when the zip is a job's py_module. Same sources, same bytes: sorted entries, the zip epoch as every mtime.
# Writes <out>/jobs/<slug>-<sha256>.zip and prints its staging key, jobs/<slug>-<sha256>.zip.
set -euo pipefail

bundle=${1:?usage: build-bundle.sh <bundle dir> [out dir]}
out=${2:-dist}

python3 - "$bundle" "$out" <<'PY'
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)  # the zip epoch: the hash depends on the sources only
MANIFEST = "manifest.json"
SKIPPED = {"tests", "__pycache__"}

bundle, out = Path(sys.argv[1]), Path(sys.argv[2])
slug = json.loads((bundle / MANIFEST).read_text())["slug"]
files = [bundle / MANIFEST, *(p for p in bundle.rglob("*.py") if not SKIPPED & set(p.relative_to(bundle).parts))]
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
    for path in sorted(files, key=lambda p: p.relative_to(bundle).as_posix()):
        info = zipfile.ZipInfo(path.relative_to(bundle).as_posix(), ZIP_TIMESTAMP)
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, path.read_bytes())
key = f"jobs/{slug}-{hashlib.sha256(buf.getvalue()).hexdigest()}.zip"
(out / key).parent.mkdir(parents=True, exist_ok=True)
(out / key).write_bytes(buf.getvalue())
print(key)
PY
