"""manifest.json of a task bundle: what is accepted, what is refused, and the ast check of its entry points."""
import pytest
from pydantic import ValidationError

from app.core.bundle_manifest import BundleManifest, hyperparameter_errors, missing_entry_points
from tests.support.bundles import SOURCES, manifest


def _preset(**over):
    return [{**manifest()["presets"][0], **over}]


def test_the_real_manifest_is_accepted():
    m = BundleManifest.model_validate(manifest())
    assert (m.slug, m.schema_, m.presets[0].name, m.serving.encoder) == ("image-classification", 1, "small", "image_encoder")
    assert m.model_dump(mode="json", by_alias=True)["schema"] == 1


def test_a_preset_without_a_base_model_or_preprocessing_trains_from_scratch():
    preset = {k: v for k, v in manifest()["presets"][0].items() if k != "base_model"} | {"preprocessing": {}}
    m = BundleManifest.model_validate(manifest(presets=[preset]))
    assert (m.presets[0].base_model, m.presets[0].preprocessing) == (None, None)


@pytest.mark.parametrize("over", [
    {"schema": 2},
    {"slug": "image_classification"},  # projects pick tasks by the Hugging Face slug
    {"data_kind": "vision"},
    {"trainer": "imgcls.train"},
    {"servable": True, "serving": None},
    {"serving": {"encoder": "image-encoder", "postprocess": "classify"}},
    {"hyperparameters": {"type": "object", "properties": {"epochs": {"type": "integr"}}}},
    {"hyperparameters": {"type": "array"}},
    {"presets": []},
    {"presets": manifest()["presets"][:1] * 2},
    {"presets": _preset(augmentation="flip")},  # not one image classification implements
    {"presets": _preset(hyperparameters={"epochs": 0})},  # outside the bundle's own schema
    {"presets": _preset(base_model={"repo_id": "x", "revision": "main"})},
    {"surprise": 1},
])
def test_a_bad_manifest_is_refused(over):
    with pytest.raises(ValidationError):
        BundleManifest.model_validate(manifest(**over))


def test_entry_points_resolve_against_the_sources_without_importing():
    m = BundleManifest.model_validate(manifest())
    assert missing_entry_points(m, SOURCES) == []
    assert missing_entry_points(m, {**SOURCES, "imgcls/train.py": b"def other(): pass\n"}) == ["imgcls.train:train"]
    assert missing_entry_points(m, {**SOURCES, "imgcls/smoke.py": b"def spec_doc(:\n"}) == ["imgcls.smoke:spec_doc"]
    package_module = {k: v for k, v in SOURCES.items() if k != "imgcls/train.py"} | {"imgcls/train/__init__.py": b"train = 1\n"}
    assert missing_entry_points(m, package_module) == []
    imported = {**SOURCES, "imgcls/smoke.py": b"from imgcls.x import spec_doc\n"}
    assert missing_entry_points(m, imported) == []


def test_hyperparameter_errors_name_the_key():
    schema = manifest()["hyperparameters"]
    assert hyperparameter_errors(schema, {"epochs": 3}) == []
    assert hyperparameter_errors(schema, {"epochs": 0})[0].startswith("epochs:")
