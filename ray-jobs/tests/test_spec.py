import json

import pytest

from ray_jobs.spec import parse_spec


def spec(**over):
    return json.dumps({"spec_version": 1, "kind": "ingest", "job_id": "j", "payload": {"bucket": "b"}, **over})


def test_the_payload_is_the_runs_params():
    assert parse_spec(spec()) == {"bucket": "b"}


@pytest.mark.parametrize("over", [{"spec_version": 2}, {"kind": "train"}])
def test_an_unknown_version_or_kind_is_refused(over):
    with pytest.raises(ValueError):
        parse_spec(spec(**over))
