#!/usr/bin/env python3
"""Render docs/feature-coverage.md from docs/feature-coverage.yaml.

    core-api/.venv/bin/python scripts/feature-coverage-report.py          # rewrite docs/feature-coverage.md
    core-api/.venv/bin/python scripts/feature-coverage-report.py --check  # exit 1 if the .md is out of date

Standard library plus PyYAML (a core-api dependency, so the core-api venv has it).
"""
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "feature-coverage.yaml"
OUT = ROOT / "docs" / "feature-coverage.md"

AREAS = ["training", "registry", "deployment", "serving", "predict", "jobs", "datasets"]
STATUSES = ["built", "partial", "missing", "deferred"]
DEFAULT_WEIGHT = 2
NOT_IN_CI = ()  # path prefixes of test suites no CI workflow runs; .github/workflows/tests.yml runs core-api, ray-jobs, ray-training and ray-serving
RANK = {"missing": 0, "partial": 1, "deferred": 2}  # within one weight, a missing feature outranks a partial one


def cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def table(header: list[str], rows: list[list]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return lines + ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]


def render(entries: list[dict]) -> str:
    out = [
        "# Feature coverage",
        "",
        "Which designed capabilities of training, the model registry, deployments, serving, the predict API, jobs and datasets are built, and which are tested.",
        "Generated from [feature-coverage.yaml](feature-coverage.yaml); do not edit by hand. Regenerate with:",
        "",
        "```bash",
        "core-api/.venv/bin/python scripts/feature-coverage-report.py",
        "```",
        "",
        "`core-api/tests/architecture/test_feature_coverage.py` keeps the manifest honest: every cited file, test function and route exists, every in-scope route is recorded, and a built feature names a test.",
        "Status: built = works and is wired; partial = works with a stated gap; missing = designed, not built; deferred = the design itself schedules it later.",
        "",
        "## Totals",
        "",
    ]
    counts = Counter((e["area"], e["status"]) for e in entries)
    rows = [[a, *[counts[a, s] for s in STATUSES], sum(counts[a, s] for s in STATUSES)] for a in AREAS]
    rows.append(["**all**", *[sum(counts[a, s] for a in AREAS) for s in STATUSES], len(entries)])
    out += table(["Area", *STATUSES, "total"], rows)
    due = [e for e in entries if e["status"] != "deferred"]
    score = sum(1 if e["status"] == "built" else 0.5 if e["status"] == "partial" else 0 for e in due)
    out += ["", f"Built share of the features that are due (partial counts half, deferred excluded): {score / len(due):.0%} of {len(due)}.", ""]

    gaps = sorted((e for e in entries if e["status"] != "built"), key=lambda e: (-e.get("weight", DEFAULT_WEIGHT), RANK[e["status"]], e["id"]))
    out += ["## Gaps, most important first", "", "Weight 3 blocks the product path or tenant isolation, 2 is a real gap, 1 is polish. Missing outranks partial, partial outranks deferred.", ""]
    out += table(
        ["Weight", "Status", "Id", "Area", "Feature", "Gap"],
        [[e.get("weight", DEFAULT_WEIGHT), e["status"], e["id"], e["area"], e["title"], e.get("gap", "")] for e in gaps],
    )

    thin = [e for e in entries if e["status"] in ("built", "partial") and e["tests"] and all(t.startswith(NOT_IN_CI) for t in e["tests"])]
    unspecific = [e for e in entries if e["status"] == "built" and e["tests"] and all("::" not in t for t in e["tests"])]
    untested = [e for e in entries if e["status"] == "built" and not e["tests"]]
    out += ["", "## Built but not (well) tested", ""]
    out += ["**Built with no test at all:** " + (", ".join(e["id"] for e in untested) or "none (the test suite refuses this)"), ""]
    out += ["**Tested only by suites CI does not run** (`.github/workflows/tests.yml` runs core-api, ray-jobs, ray-training, ray-serving and the Postgres isolation matrix):", ""]
    out += table(["Id", "Status", "Feature", "Tests"], [[e["id"], e["status"], e["title"], ", ".join(e["tests"])] for e in thin]) if thin else ["none"]
    out += ["", "**Built, but the tests cited are whole files, not named behaviours** (look here for thin coverage):", ""]
    out += table(["Id", "Feature", "Tests"], [[e["id"], e["title"], ", ".join(e["tests"])] for e in unspecific]) if unspecific else ["none"]

    notes = [e for e in entries if "unverif" in (e.get("note", "") + e.get("gap", "")).lower() or "not checked" in e.get("note", "").lower()]
    out += ["", "## Could not be verified from the repo", ""]
    out += table(["Id", "Feature", "Why"], [[e["id"], e["title"], (e.get("note") or e.get("gap", ""))] for e in notes]) if notes else ["none"]

    out += ["", "## All features", ""]
    for area in AREAS:
        out += [f"### {area}", ""]
        out += table(
            ["Id", "Status", "Feature", "Design source", "Tests"],
            [[e["id"], e["status"], e["title"], e["source"], len(e["tests"])] for e in entries if e["area"] == area],
        )
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    text = render(yaml.safe_load(SRC.read_text())["entries"])
    if "--check" in sys.argv:
        return 0 if OUT.exists() and OUT.read_text() == text else 1
    OUT.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
