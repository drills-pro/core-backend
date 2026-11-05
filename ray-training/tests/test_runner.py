"""report.json is written whatever happens, with the right failure code."""
import json

import jsonschema
import pytest

from ray_training.constants import REPORT_KEY, FailCode
from ray_training.errors import BadData, Unsupported
from ray_training.runner import failure_code, run
from ray_training.spec import parse_spec
from tests.support import MemStore, classification_rows, fake_get, spec_text


from tests.support import REPORT_SCHEMA  # noqa: E402


def boom(exc):
    def trainer(*_):
        raise exc
    return trainer


@pytest.mark.parametrize("exc,code", [
    (BadData("x"), FailCode.BAD_DATA), (Unsupported("x"), FailCode.UNSUPPORTED), (TimeoutError(), FailCode.TIMEOUT),
    (RuntimeError("CUDA out of memory. Tried to allocate"), FailCode.OOM), (MemoryError(), FailCode.OOM),
    (ValueError("anything else"), FailCode.INTERNAL)])
def test_each_failure_is_coded_and_still_reported(exc, code):
    rows, files = classification_rows()
    store = MemStore()
    report = run(parse_spec(spec_text("image-classification", "m", rows)), store, fake_get(rows, files), boom(exc))
    written = json.loads(store.puts[REPORT_KEY])
    jsonschema.validate(written, REPORT_SCHEMA)
    assert report["status"] == written["status"] == "failed" and written["code"] == code
    assert written["dataset"]["lakefs_commit"] == "c0ffee" and written["metrics"] == {}


def test_torch_oom_class_name_is_oom():
    class OutOfMemoryError(RuntimeError):
        pass

    assert failure_code(OutOfMemoryError("x")) == FailCode.OOM


def test_bad_data_in_the_index_fails_before_training():
    rows, files = classification_rows()
    store = MemStore()
    spec = parse_spec(spec_text("image-classification", "m", rows))
    report = run(spec, store, fake_get(rows, files, manifest_rows=rows[:-1]))  # manifest lacks one image
    assert report["code"] == FailCode.BAD_DATA and REPORT_KEY in store.puts
