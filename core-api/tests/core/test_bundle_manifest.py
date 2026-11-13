"""manifest.json of a task bundle: what is accepted, what is refused, and the ast check of its entry points."""
import pytest
from pydantic import ValidationError

from app.core.bundle_manifest import BundleManifest, missing_entry_points
from tests.support.bundles import SOURCES, manifest


def test_the_documented_manifest_is_accepted():
    m = BundleManifest.model_validate(manifest())
    assert (m.slug, m.schema_, m.presets[0].name) == ("image-classification", 1, "small")
    assert m.model_dump(mode="json", by_alias=True)["schema"] == 1


@pytest.mark.parametrize("over", [
    {"schema": 2},
    {"slug": "image_classification"},  # projects pick tasks by the Hugging Face slug
    {"data_kind": "vision"},
    {"trainer": "imgcls.train"},
    {"servable": True, "serving": None},
    {"hyperparameters": {"type": "object", "properties": {"epochs": {"type": "integr"}}}},
    {"hyperparameters": {"type": "array"}},
    {"presets": []},
    {"presets": [manifest()["presets"][0]] * 2},
    {"presets": [{**manifest()["presets"][0], "augmentation": "flip"}]},  # not one image classification implements
    {"surprise": 1},
])
def test_a_bad_manifest_is_refused(over):
    with pytest.raises(ValidationError):
        BundleManifest.model_validate(manifest(**over))


def test_entry_points_resolve_against_the_sources_without_importing():
    m = BundleManifest.model_validate(manifest())
    assert missing_entry_points(m, SOURCES) == []
    assert missing_entry_points(m, {**SOURCES, "imgcls/train.py": b"def other(): pass\n"}) == ["imgcls.train:train"]
    assert missing_entry_points(m, {**SOURCES, "imgcls/smoke.py": b"def run(:\n"}) == ["imgcls.smoke:run"]
    package_module = {k: v for k, v in SOURCES.items() if k != "imgcls/train.py"} | {"imgcls/train/__init__.py": b"train = 1\n"}
    assert missing_entry_points(m, package_module) == []
