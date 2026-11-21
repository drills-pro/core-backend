"""The three Ray packages (ray-jobs, ray-training, ray-serving) ship as separate zips, so each keeps its own copy of what they share
with core-api and with each other: the layout keys, the vocabularies, the staging store, the spec envelope check. These tests fail
when a copy drifts. (ray_serving's numpy-bound code is cross-checked in ray-serving/tests, which runs where numpy is installed.)"""
import base64
import os

import pytest

from app.core import task_catalog
from app.integrations import job_spec
from app.integrations.data_plane import job_submit, layout
from app.integrations.data_plane.train_report import TRAIN_REPORT_SCHEMA_VERSION, FailureCode, TrainOutcome
from app.modules.user.training import constants as training
from ray_core import constants as core
from ray_core import spec as core_spec
from ray_core import store as core_store
from ray_jobs.common import constants as jobs
from ray_jobs.common import enums as jobs_enums
from ray_jobs.common import store as jobs_store
from ray_jobs import spec as jobs_spec
from ray_serving import batch_spec
from ray_serving import constants as serving
from ray_serving import store as serving_store


def values(enum) -> set[str]:
    return {m.value for m in enum}


# ---- mirrored constants and enums ----


def test_every_package_names_the_layout_keys_as_core_api_does():
    for package in (jobs, core, serving):
        assert (package.SMOKE_KEY, package.REPORT_KEY) == (layout.SMOKE_KEY, layout.REPORT_KEY), package.__name__


def test_every_package_reads_the_spec_version_core_api_writes():
    assert jobs.SPEC_VERSION == core.SPEC_VERSION == serving.SPEC_VERSION == job_spec.SPEC_VERSION


def test_the_trainer_and_the_ingest_job_agree_on_the_report_version_and_dataset_columns():
    assert core.REPORT_SCHEMA_VERSION == TRAIN_REPORT_SCHEMA_VERSION
    assert (jobs.TEXT_COLUMN, jobs.LABEL_COLUMN) == (core.TEXT_COLUMN, core.LABEL_COLUMN) == (task_catalog.TEXT_COLUMN, task_catalog.LABEL_COLUMN)


def test_the_trainers_index_vocabulary_is_the_layouts_and_the_ingest_jobs():
    assert tuple(core.Col) == layout.INDEX_COLUMNS == tuple(jobs_enums.Col)
    assert tuple(core.Box) == layout.BOX_FIELDS == tuple(jobs_enums.Box)
    assert values(core.Split) == values(layout.Split)


def test_the_trainers_report_vocabulary_is_core_apis():
    assert values(core.Outcome) == values(TrainOutcome)
    assert values(core.FailCode) == values(FailureCode) == values(training.FailureCode)
    assert values(core.EventType) == values(training.EventType)


def test_the_trainers_recipe_enums_are_the_catalogs():
    assert values(core.LrSchedule) == values(task_catalog.LrSchedule)
    assert values(core.Augmentation) == values(task_catalog.Augmentation)


def test_the_http_timeout_is_one_value():
    assert jobs.HTTP_TIMEOUT_SECONDS == core.HTTP_TIMEOUT_SECONDS == serving.HTTP_TIMEOUT_SECONDS


# ---- the spec envelope ----

PARSERS = [
    pytest.param(jobs_spec.parse_spec, job_spec.JobKind.INGEST, "ray_jobs", id="ray_jobs"),
    pytest.param(core_spec.parse_spec, job_spec.JobKind.TRAIN, "ray_core", id="ray_core"),
    pytest.param(batch_spec.parse_spec, job_spec.JobKind.PREDICT_BATCH, "predict_batch", id="ray_serving"),
]


@pytest.mark.parametrize("parse, kind, runner", PARSERS)
def test_a_package_refuses_another_spec_version_the_same_way(parse, kind, runner):
    with pytest.raises(ValueError, match=rf"^job spec version 2 not supported, expected {job_spec.SPEC_VERSION}$"):
        parse(f'{{"spec_version": 2, "kind": "{kind}"}}')


@pytest.mark.parametrize("parse, kind, runner", PARSERS)
def test_a_package_runs_only_its_own_kind(parse, kind, runner):
    for other in job_spec.JobKind:
        if other != kind:
            with pytest.raises(ValueError, match=rf"^job kind '{other}' is not run by {runner}$"):
                parse(f'{{"spec_version": {job_spec.SPEC_VERSION}, "kind": "{other}"}}')


# ---- the staging store ----

STORES = [pytest.param(m, id=m.__name__) for m in (jobs_store, core_store, serving_store)]


def test_the_checksum_settings_are_core_apis():
    for module in (jobs_store, core_store, serving_store):
        assert module.CHECKSUM_ENV == tuple(job_submit.S3_COMPATIBLE_ENV), module.__name__
        assert {job_submit.S3_COMPATIBLE_ENV[n] for n in module.CHECKSUM_ENV} == {module.CHECKSUM_WHEN_REQUIRED}


@pytest.mark.parametrize("store", STORES)
def test_every_s3_store_turns_checksums_off_unless_the_job_set_them(store, monkeypatch):
    for name in store.CHECKSUM_ENV:
        monkeypatch.delenv(name, raising=False)
    store.S3Store("bucket", "p/", "http://127.0.0.1:9", "key", "secret")
    assert {job_submit.S3_COMPATIBLE_ENV[n] for n in store.CHECKSUM_ENV} == {store.CHECKSUM_WHEN_REQUIRED}
    monkeypatch.setenv(store.CHECKSUM_ENV[0], "when_supported")
    store.S3Store("bucket", "p/", "http://127.0.0.1:9", "key", "secret")
    assert os.environ[store.CHECKSUM_ENV[0]] == "when_supported"


@pytest.mark.parametrize("store", STORES)
def test_every_package_reads_a_data_url_through_http_get(store):
    body = b"fixture bytes"
    assert store.http_get("data:application/octet-stream;base64," + base64.b64encode(body).decode()) == body


@pytest.mark.parametrize("store", [core_store, serving_store], ids=["ray_core", "ray_serving"])
def test_the_local_store_writes_under_the_prefix_and_is_chosen_by_the_file_scheme(store, tmp_path, monkeypatch):
    monkeypatch.setenv("STAGING_ENDPOINT", f"{store.LOCAL_SCHEME}{tmp_path}")
    chosen = store.store_from_env("bucket", "run/1/")
    assert type(chosen) is store.DirStore
    chosen.put("out/report.json", b"{}")
    assert (tmp_path / "run/1/out/report.json").read_bytes() == b"{}"
    monkeypatch.setenv("STAGING_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("STAGING_ACCESS_KEY", "k")
    monkeypatch.setenv("STAGING_SECRET_KEY", "s")
    s3 = store.store_from_env("bucket", "run/1/")
    assert (type(s3), s3.bucket, s3.prefix) == (store.S3Store, "bucket", "run/1/")
