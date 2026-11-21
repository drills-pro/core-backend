"""Task bundles in staging: what register accepts, and how bundle and core smokes run."""
import asyncio
import hashlib
import io
import json
import zipfile
import pytest

from app.core.enums import JobPackageName, PackageAuditAction, PackageKind, PackageStatus
from app.core.job_packages import REQUIREMENTS_CONFLICT, JobPackage
from app.core.problem import Problem
from app.core.requirements import requirements_sha256
from app.integrations.data_plane import job_catalog, job_package, lakefs, package_zip, task_bundle
from tests.support.packages import FakeS3, PackageDb, package_row
from tests.support.bundles import SLUG, bundle_zip, manifest

CORE_SHA, OTHER = "a" * 64, "b" * 64


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_a_good_bundle_gives_its_manifest():
    data = bundle_zip()
    assert task_bundle.check_bundle(data, _sha(data)).slug == SLUG


@pytest.mark.parametrize("data, code", [
    (bundle_zip(extra={"imgcls/model.bin": b"x"}), "invalid_package"),
    (bundle_zip(extra={"../outside.py": b""}), "invalid_package"),
    (bundle_zip(manifest(contract=2)), "invalid_manifest"),
    (bundle_zip(manifest(trainer="imgcls.train:nope")), "invalid_manifest"),
    (bundle_zip(manifest(data_kind="vision")), "invalid_manifest"),
    (bundle_zip(manifest(serving={"encoder": "no_such_encoder", "postprocess": "classify"})), "invalid_manifest"),
    (b"not a zip", "invalid_package"),
])
def test_a_bad_bundle_is_refused(data, code):
    with pytest.raises(Problem) as e:
        task_bundle.check_bundle(data, _sha(data))
    assert e.value.status_code == 422 and e.value.code == code


def test_manifest_requirements_are_validated_and_normalised():
    data = bundle_zip(manifest(requirements=["Xgboost==3.2.0", "scipy==1.14.0"]))
    assert task_bundle.check_bundle(data, _sha(data)).requirements == ["xgboost==3.2.0", "scipy==1.14.0"]
    data = bundle_zip(manifest())
    assert task_bundle.check_bundle(data, _sha(data)).requirements == []


@pytest.mark.parametrize("requirements", [["xgboost>=3"], ["a==1", "A==1"], ["--index-url x"], "xgboost==3.2.0", [1]])
def test_a_manifest_with_unpinned_requirements_is_refused_with_its_own_code(requirements):
    data = bundle_zip(manifest(requirements=requirements))
    with pytest.raises(Problem) as e:
        task_bundle.check_bundle(data, _sha(data))
    assert (e.value.status_code, e.value.code) == (422, package_zip.INVALID_REQUIREMENTS)


def test_a_bundle_without_a_manifest_or_with_a_wrong_hash_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("imgcls/train.py", "")
    with pytest.raises(Problem, match="manifest"):
        task_bundle.check_bundle(buf.getvalue(), _sha(buf.getvalue()))
    with pytest.raises(Problem, match="hash"):
        task_bundle.check_bundle(bundle_zip(), OTHER)


@pytest.fixture
def staged(monkeypatch):
    """register_staged against a staging bucket and the job_packages tables."""
    bucket, tables = FakeS3(), PackageDb()
    monkeypatch.setattr(lakefs, "_s3", lambda secret, endpoint=None: bucket)
    monkeypatch.setattr(task_bundle, "get_pool", tables.pool())

    def stage(data, key=None):
        key = key or f"jobs/{SLUG}-{_sha(data)}.zip"
        bucket.objects[key] = data
        return key

    return stage, tables


def test_a_staged_bundle_is_registered_as_task_slug_and_audited(staged):
    stage, tables = staged
    key = stage(bundle_zip())
    package = asyncio.run(task_bundle.register_staged(key, "ci"))
    row = tables.rows[(f"task_{SLUG}", package.sha256)]
    assert (row["s3_key"], row["manifest"]["schema"], row["published_by"], row["requirements"]) == (key, 1, "ci", [])
    assert package.kind == PackageKind.TASK and tables.audit == [(f"task_{SLUG}", package.sha256, PackageAuditAction.REGISTER, "ci")]


def test_the_manifest_requirements_are_stored_with_the_bundle(staged):
    stage, tables = staged
    asyncio.run(task_bundle.register_staged(stage(bundle_zip(manifest(requirements=["xgboost==3.2.0"]))), "ci"))
    (row,) = tables.rows.values()
    assert row["requirements"] == ["xgboost==3.2.0"]


@pytest.mark.parametrize("key", ["jobs/image-classification.zip", "other/image-classification-" + "a" * 64 + ".zip"])
def test_a_key_that_is_not_a_bundle_key_is_refused(staged, key):
    with pytest.raises(Problem, match="expected a key"):
        asyncio.run(task_bundle.register_staged(key, "ci"))


def test_the_key_slug_must_be_the_manifest_slug(staged):
    stage, tables = staged
    data = bundle_zip()
    key = stage(data, f"jobs/object-detection-{_sha(data)}.zip")
    with pytest.raises(Problem, match="manifest"):
        asyncio.run(task_bundle.register_staged(key, "ci"))
    assert tables.rows == {}


def _bundle(sha=OTHER, status=PackageStatus.CANDIDATE):
    return JobPackage.model_validate(package_row(f"task_{SLUG}", sha, "k", "ci", status) | {"kind": PackageKind.TASK, "manifest": manifest()})


@pytest.fixture
def smoke(monkeypatch):
    core = JobPackage.model_validate(package_row(JobPackageName.RAY_TRAINING, CORE_SHA, "c", "ci", PackageStatus.STABLE))
    sent = {}

    async def submit(**kw):
        sent.update(kw)

    async def resolve_stable(name):
        return core

    monkeypatch.setattr(job_package.job_submit, "submit", submit)
    monkeypatch.setattr(job_package, "resolve_stable", resolve_stable)
    monkeypatch.setattr(job_package.lakefs, "_s3", lambda secret, endpoint=None: FakeS3())

    def run(package, stable_bundles=()):
        tables = PackageDb()
        for p in (core, package, *stable_bundles):  # the rows a run reads back: its own, the core's, the stable bundles'
            tables.rows[(p.name, p.sha256)] = p.model_dump()
        monkeypatch.setattr(job_package, "get_pool", tables.pool())
        asyncio.run(job_package.start_smoke(package))
        return sent, json.loads(sent["spec_json"])

    return run, core


def test_a_bundle_smoke_runs_the_bundle_function_on_the_stable_core(smoke):
    run, _ = smoke
    sent, spec = run(_bundle())
    assert sent["module"] == job_catalog.SMOKE_ENTRYPOINTS[JobPackageName.RAY_TRAINING] == "ray_core.entrypoints.smoke_all"
    assert spec["bundles"] == [{"smoke": "imgcls.smoke:spec_doc", "requires": manifest()["requires"]}]
    assert (sent["package_url"], sent["bundle_urls"]) == ("http://ray-side/c", ["http://ray-side/k"])
    assert sent["metadata"]["core_sha256"] == CORE_SHA


def test_a_core_smoke_runs_every_stable_bundle_on_the_candidate_core(smoke):
    run, core = smoke
    candidate = core.model_copy(update={"sha256": "c" * 64, "s3_key": "cand", "status": PackageStatus.CANDIDATE})
    sent, spec = run(candidate, [_bundle(OTHER, PackageStatus.STABLE), _bundle("d" * 64, PackageStatus.STABLE)])
    assert sent["package_url"] == "http://ray-side/cand" and len(sent["bundle_urls"]) == len(spec["bundles"]) == 2
    sent, spec = run(candidate)
    assert spec["bundles"] == [] and sent["bundle_urls"] == []  # nothing stable yet: nothing to break


def _with(package, *requirements):
    return package.model_copy(update={"requirements": list(requirements)})


def test_a_bundle_smoke_installs_the_cores_and_the_bundles_pins_merged(smoke):
    run, core = smoke
    core.requirements = ["torch==2.11.0", "numpy==2.1.0"]  # the stable core the smoke resolves
    sent, _ = run(_with(_bundle(), "numpy==2.1.0", "xgboost==3.2.0"))
    assert sent["requirements"] == ["numpy==2.1.0", "torch==2.11.0", "xgboost==3.2.0"]
    assert sent["metadata"]["requirements_sha256"] == requirements_sha256(sent["requirements"])


def test_a_core_smoke_installs_the_candidate_cores_pins_with_every_stable_bundles(smoke):
    run, core = smoke
    candidate = _with(core, "torch==2.11.0")
    sent, _ = run(candidate, [_with(_bundle(OTHER, PackageStatus.STABLE), "xgboost==3.2.0")])
    assert sent["requirements"] == ["torch==2.11.0", "xgboost==3.2.0"]


def test_a_smoke_without_pins_installs_nothing_and_records_no_hash(smoke):
    run, _ = smoke
    sent, _ = run(_bundle())
    assert sent["requirements"] == [] and "requirements_sha256" not in sent["metadata"]


def test_a_smoke_of_conflicting_pins_does_not_start(smoke):
    run, core = smoke
    core.requirements = ["numpy==2.1.0"]
    with pytest.raises(Problem) as err:
        run(_with(_bundle(), "numpy==2.2.0"))
    assert (err.value.status_code, err.value.code) == (409, REQUIREMENTS_CONFLICT)
