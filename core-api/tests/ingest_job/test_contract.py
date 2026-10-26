"""The contract between core-api and the ray_jobs package (top-level ray-jobs/): the job mirrors core-api's constants
and enums instead of importing them, so these tests fail when the two drift. The job's own logic is tested in ray-jobs/tests."""
import uuid
from importlib import import_module

from app.core.enums import TaskType
from app.core.settings import DatasetLimits, settings
from app.integrations import job_spec
from app.integrations.data_plane import job_catalog, layout, report as report_model
from app.integrations.job_spec import IngestBase, IngestHf, IngestPayload, IngestSpec, Resources
from app.modules.user.datasets.constants import ImportSource
from ray_jobs.common import constants as job
from ray_jobs.common.constants import SPEC_VERSION
from ray_jobs.common.enums import BOX_FIELDS, INDEX_COLUMNS, Code, ColumnType, JobKind, Outcome, Param, Source
from ray_jobs.common.limits import Limits
from ray_jobs.common.report import Report
from ray_jobs.common.rows import image_row
from ray_jobs.common.stats import compute_stats
from ray_jobs.spec import parse_spec

JPG = b"\xff\xd8\xff" + b"x" * 20


def test_constants_mirror_the_layout_and_task_types():
    assert (job.UPLOAD_KEY, job.REPORT_KEY, job.OUT_PREFIX, job.OBJECTS_PREFIX) == (
        layout.UPLOAD_KEY, layout.REPORT_KEY, layout.OUT_PREFIX, layout.OBJECTS_PREFIX)
    assert (job.INDEX_KEY, job.DATA_KEY, job.SCHEMA_KEY) == (layout.INDEX_KEY, layout.DATA_KEY, layout.SCHEMA_KEY)
    assert INDEX_COLUMNS == layout.INDEX_COLUMNS and BOX_FIELDS == layout.BOX_FIELDS
    assert job.SPLITS == tuple(s.value for s in layout.Split)
    assert (job.IMAGE_CLASSIFICATION, job.OBJECT_DETECTION) == (TaskType.IMAGE_CLASSIFICATION, TaskType.OBJECT_DETECTION)
    assert (job.TABULAR_CLASSIFICATION, job.TABULAR_REGRESSION) == (TaskType.TABULAR_CLASSIFICATION, TaskType.TABULAR_REGRESSION)
    assert job.REPORT_SCHEMA_VERSION == report_model.REPORT_SCHEMA_VERSION and job.MAX_REPORT_ISSUES == report_model.MAX_REPORT_ISSUES


def test_every_job_issue_code_is_in_the_report_schema_and_a_report_validates():
    assert {c.value for c in Code} <= {c.value for c in report_model.IssueCode}
    rep = Report()
    rep.warn(Code.DUPLICATE_SKIPPED, "dup", "a")
    stats = compute_stats([image_row("a/1.jpg", JPG, "a", None, "train")], job.IMAGE_CLASSIFICATION, None, 1)
    parsed = report_model.ImportReport.model_validate(rep.to_dict(stats, "abc", "mit"))
    assert parsed.outcome == "ok" and parsed.stats.classes == {"a": 1}
    rep.error(Code.NO_DATA, "x")
    assert report_model.ImportReport.model_validate(rep.to_dict(stats)).stats is None


def test_a_capped_report_still_validates():
    rep = Report()
    for _ in range(job.MAX_REPORT_ISSUES + 5):
        rep.error(Code.CORRUPT_IMAGE, "x")
    report_model.ImportReport.model_validate(rep.to_dict())


def test_enums_mirror_core_api():
    assert {m.value for m in Source} == {m.value for m in ImportSource}
    assert {m.value for m in Outcome} == {m.value for m in report_model.Outcome}
    assert {m.value for m in ColumnType} == {m.value for m in layout.ColumnType}


def test_limits_cover_the_settings_knobs():
    assert set(Limits.__dataclass_fields__) <= set(DatasetLimits.model_fields)


def test_job_params_keys_cover_what_core_api_sends():
    nested = {"base": IngestBase, "hf": IngestHf}
    keys = set(IngestPayload.model_fields) | {k for m in nested.values() for k in m.model_fields}
    assert keys <= {p.value for p in Param}


def test_the_job_reads_the_spec_core_api_writes():
    spec = IngestSpec(job_id="j", tenant_id=uuid.uuid4(), resources=Resources(cpu=1, memory_bytes=1), payload=IngestPayload(
        bucket="b", prefix="p/", source=ImportSource.UPLOAD, task=TaskType.IMAGE_CLASSIFICATION, limits=settings.dataset_limits))
    params = parse_spec(spec.model_dump_json())
    assert params["prefix"] == "p/" and Limits.from_dict(params["limits"])
    assert (SPEC_VERSION, set(JobKind)) == (job_spec.SPEC_VERSION, set(job_spec.JobKind))
    assert JobKind.INGEST == job_spec.JobKind.INGEST


def test_every_job_type_has_an_entrypoint_module_that_runs_as_main():
    for kind in job_catalog.JobType:
        assert callable(import_module(f"{job_catalog.ENTRYPOINTS}.{kind.value}").extract), kind


def test_every_source_and_task_picks_a_job_type():
    picked = {(s, t): job_catalog.ingest_job_type(s, t) for s in ImportSource for t in TaskType}
    assert set(picked.values()) == set(job_catalog.JobType)
    assert picked[(ImportSource.UPLOAD, TaskType.OBJECT_DETECTION)] == job_catalog.JobType.UPLOAD_IMAGES
    assert picked[(ImportSource.UPLOAD, TaskType.TABULAR_REGRESSION)] == job_catalog.JobType.UPLOAD_CSV
    assert picked[(ImportSource.HUGGINGFACE, TaskType.IMAGE_CLASSIFICATION)] == job_catalog.JobType.HUGGINGFACE
