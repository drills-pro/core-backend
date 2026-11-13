"""A task bundle's `manifest.json` (planning/25 "Bundle format"): what the bundle trains, how it is smoked and served, the
JSON Schema of its hyperparameters and the presets it owns. Read from the zip at register and stored on its job_packages
row; promotion derives the task_types and task_presets rows from it.

Entry points are checked against the zip's sources with `ast`, never imported: core-api does not run bundle code.
"""
import ast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.enums import TaskType
from app.core.presets import NAME_PATTERN, PresetBody
from app.core.task_catalog import DataKind
from app.core.task_types import TRAINER_PATTERN

MANIFEST_FILE = "manifest.json"
MANIFEST_SCHEMA = 1  # the version of this format; a bundle names the one it is written in
CORE_CONTRACT = 1  # the ray_core contract this core-api serves; a bundle written for another one is refused at register
JSON_OBJECT = "object"


class ServingAdapter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: str = Field(pattern=TRAINER_PATTERN)  # "module:Class" inside the bundle
    adapter_version: int = Field(ge=1)


class ManifestPreset(PresetBody):
    """One preset the bundle owns: a task_presets row once the bundle is promoted."""

    name: str = Field(pattern=NAME_PATTERN)


class BundleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: int = Field(alias="schema", ge=1, le=MANIFEST_SCHEMA)
    slug: TaskType  # the task it trains; projects pick it by this slug
    display_name: str = Field(min_length=1)
    data_kind: DataKind
    contract: int = Field(ge=1)  # the ray_core contract the bundle was written for
    trainer: str = Field(pattern=TRAINER_PATTERN)
    smoke: str = Field(pattern=TRAINER_PATTERN)
    serving: ServingAdapter | None = None
    servable: bool = False
    monitor: str | None = Field(default=None, min_length=1)  # the metric that picks the best checkpoint
    hyperparameters: dict  # JSON Schema of what a training request may set
    presets: list[ManifestPreset] = Field(min_length=1)
    requires: list[str] = []  # distributions the bundle imports; the smoke checks the image has them

    @field_validator("hyperparameters")
    @classmethod
    def _is_json_schema(cls, schema: dict) -> dict:
        try:
            validator_for(schema, default=Draft202012Validator).check_schema(schema)
        except SchemaError as e:
            raise ValueError(f"hyperparameters is not a valid JSON Schema: {e.message}") from None
        if schema.get("type") != JSON_OBJECT:
            raise ValueError("hyperparameters must describe an object")
        return schema

    @model_validator(mode="after")
    def _consistent(self):
        if self.servable and self.serving is None:
            raise ValueError("a servable bundle needs a serving adapter")
        names = [p.name for p in self.presets]
        if len(names) != len(set(names)):
            raise ValueError("preset names must be unique")
        for p in self.presets:
            p.check_task(self.slug)
        return self

    def entry_points(self) -> list[str]:
        return [self.trainer, self.smoke] + ([self.serving.adapter] if self.serving else [])


def _defines(source: bytes, name: str) -> bool:
    """Whether the module's top level binds `name` (a def, class, assignment or import)."""
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            return True
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)) and any((a.asname or a.name.split(".")[0]) == name for a in node.names):
            return True
    return False


def missing_entry_points(manifest: BundleManifest, sources: dict[str, bytes]) -> list[str]:
    """The manifest's `module:attr` targets that no source defines. `sources` maps a path relative to the package root
    ("imgcls/train.py") to its bytes; a module may be a file or a package's __init__.py."""
    missing = []
    for target in manifest.entry_points():
        module, _, attr = target.partition(":")
        path = module.replace(".", "/")
        source = sources.get(f"{path}.py", sources.get(f"{path}/__init__.py"))
        try:
            found = source is not None and _defines(source, attr)
        except SyntaxError:
            found = False
        if not found:
            missing.append(target)
    return missing
