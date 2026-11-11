"""/v1/public: no credentials at all, paging bounds on the project list, and the three listings reach their service call."""
import uuid

import pytest

from app.modules.user.public import routes, service
from tests.support import make_client

PROJECT = uuid.uuid4()


@pytest.fixture
def seen(monkeypatch):
    calls = []

    def fake(name):
        async def run(*args):
            calls.append((name, args))
            return []

        return run

    for name in ("list_projects", "list_datasets", "list_models"):
        monkeypatch.setattr(service, name, fake(name))
    return calls


@pytest.fixture
def client():
    return make_client(routes.router, headers={})


def test_the_three_listings_answer_without_credentials(client, seen):
    assert client.get("/v1/public/projects?limit=5&offset=10").json() == []
    assert client.get(f"/v1/public/projects/{PROJECT}/datasets").json() == []
    assert client.get(f"/v1/public/projects/{PROJECT}/models").json() == []
    assert seen == [("list_projects", (5, 10)), ("list_datasets", (PROJECT,)), ("list_models", (PROJECT,))]


@pytest.mark.parametrize("qs", ["limit=0", "limit=201", "offset=-1"])
def test_paging_is_bounded(client, seen, qs):
    assert client.get(f"/v1/public/projects?{qs}").status_code == 422 and not seen
