"""Rewrite contracts/job-spec.v1.json from the models in core-api/app/integrations/job_spec.py.

Run from core-api/ with its venv: APP_ENV=test PYTHONPATH=. .venv/bin/python ../scripts/gen-job-spec-schema.py
"""
import json
from pathlib import Path

from app.integrations.job_spec import JOB_SPEC

OUT = Path(__file__).parents[1] / "contracts" / "job-spec.v1.json"
OUT.write_text(json.dumps(JOB_SPEC.json_schema(), indent=2, sort_keys=True) + "\n")
