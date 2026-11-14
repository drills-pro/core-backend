"""The committed pins of planning/26 (scripts/lock.sh output) agree with core-api's rules, offline."""
import json
import re
from pathlib import Path

import pytest

from app.core.requirements import merge_requirements, parse_lock

ROOT = Path(__file__).parents[3]
CORE_LOCK = ROOT / "ray-training" / "ray_core" / "requirements.lock"
MANIFESTS = sorted((ROOT / "task-bundles" / "tasks").glob("*/manifest.json"))
CUDA_TAG = "+cu128"  # the one torch build for every job (planning/26 point 4)
PROTOBUF_BELOW = 6  # ray[serve] pins protobuf 5.x; protobuf 6+ breaks the gencode of the 5.x runtime


def _name(dist: str) -> str:
    return re.sub(r"[-_.]+", "-", dist.lower())


def _pins(requirements: list[str]) -> dict[str, str]:
    return dict(r.split("==", 1) for r in requirements)


def _manifest(path: Path) -> dict:
    return json.loads(path.read_text())


CORE = parse_lock(CORE_LOCK.read_text())
BUNDLES = {_manifest(p)["slug"]: _manifest(p) for p in MANIFESTS}


def test_there_are_bundles_to_check():
    assert CORE and len(BUNDLES) >= 4


@pytest.mark.parametrize("slug", BUNDLES)
def test_each_bundle_list_is_valid_and_agrees_with_the_core_lock(slug):
    requirements = BUNDLES[slug]["requirements"]
    assert parse_lock("\n".join(requirements)) == requirements  # exact pins, normalised, no duplicates
    assert merge_requirements(CORE, requirements)  # a different pin for one package would raise (409 at run time)
    core = _pins(CORE)
    assert not {n: v for n, v in _pins(requirements).items() if n in core and core[n] != v}


def test_the_bundles_agree_with_each_other():  # a core smoke installs every stable bundle in one environment
    assert merge_requirements(CORE, *(b["requirements"] for b in BUNDLES.values()))


@pytest.mark.parametrize("slug", BUNDLES)
def test_every_required_distribution_is_pinned(slug):
    pinned = set(_pins(CORE)) | set(_pins(BUNDLES[slug]["requirements"]))
    assert {_name(d) for d in BUNDLES[slug]["requires"]} <= pinned


def test_torch_is_the_cuda_build():
    pins = _pins(CORE)
    assert pins["torch"].endswith(CUDA_TAG) and pins["torchvision"].endswith(CUDA_TAG)


def test_protobuf_is_below_6():
    assert int(_pins(CORE)["protobuf"].split(".")[0]) < PROTOBUF_BELOW


def test_ray_is_in_no_list():  # it is in the image; a pin would install a second one
    for requirements in [CORE, *(b["requirements"] for b in BUNDLES.values())]:
        assert not {n for n in _pins(requirements) if n == "ray" or n.startswith("ray-")}
