import pytest

from objdet.smoke import tiny


@pytest.fixture(scope="session")
def tiny_rtdetr(tmp_path_factory):
    path = tmp_path_factory.mktemp("rtdetr")
    tiny(2).save_pretrained(path)
    return path
