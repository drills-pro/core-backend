"""Rewrite contracts/job-spec.v1.json from the models in core-api/app/integrations/job_spec.py.

Run from core-api/ with its venv: APP_ENV=test PYTHONPATH=. .venv/bin/python ../scripts/gen-job-spec-schema.py
"""
import json
from pathlib import Path

import app.integrations.data_plane  # noqa: F401 - loads the gateway first: job_spec and data_plane import each other
from app.integrations.job_spec import SPEC_ADAPTER

OUT = Path(__file__).parents[1] / "contracts" / "job-spec.v1.json"
OUT.write_text(json.dumps(SPEC_ADAPTER.json_schema(), indent=2, sort_keys=True) + "\n")
