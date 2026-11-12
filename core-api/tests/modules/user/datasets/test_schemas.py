"""Import params that end up in URLs and in jsonb are validated at the boundary."""
import pytest
from pydantic import ValidationError

from app.modules.user.datasets.constants import PARAM_MAX_LENGTH
from app.modules.user.datasets.schemas import ImportParams

GOOD_REPOS = ["imdb", "user/some-dataset", "org/data.set_v2"]
BAD_REPOS = ["../../api/models/x", "a/b/c", "a/../b", "../x", "a?b=1", "a#frag", "a b", "a/", "/a", "x" * (PARAM_MAX_LENGTH + 1), ".."]
GOOD_REVISIONS = ["main", "refs/convert/parquet", "0123456789abcdef0123456789abcdef01234567", "v1.0"]
BAD_REVISIONS = ["a/../b", "..", "main?x=1", "a#b", "a b", "x" * (PARAM_MAX_LENGTH + 1), "/main"]


@pytest.mark.parametrize("repo", GOOD_REPOS)
def test_repo_ids_are_accepted(repo):
    assert ImportParams(repo=repo).repo == repo


@pytest.mark.parametrize("repo", BAD_REPOS)
def test_repo_ids_that_reach_other_hub_paths_are_refused(repo):
    with pytest.raises(ValidationError):
        ImportParams(repo=repo)


@pytest.mark.parametrize("revision", GOOD_REVISIONS)
def test_revisions_are_accepted(revision):
    assert ImportParams(revision=revision).revision == revision


@pytest.mark.parametrize("revision", BAD_REVISIONS)
def test_revisions_that_are_not_ref_names_are_refused(revision):
    with pytest.raises(ValidationError):
        ImportParams(revision=revision)


@pytest.mark.parametrize("field", ["target", "column"])
def test_column_names_are_length_bounded(field):
    ImportParams(**{field: "y" * PARAM_MAX_LENGTH})
    with pytest.raises(ValidationError):
        ImportParams(**{field: "y" * (PARAM_MAX_LENGTH + 1)})


def test_a_dataset_of_a_project_without_a_task_type_still_serialises():
    from datetime import UTC, datetime
    import uuid

    from app.modules.user.datasets.schemas import DatasetSummary

    row = {"id": uuid.uuid4(), "name": "d", "task_type": None, "created_at": datetime.now(UTC), "deleted_at": None, "published_version_id": None, "data_categories": []}
    assert DatasetSummary.model_validate(row).task_type is None
