import pytest

from app.core.schemas import slugify


@pytest.mark.parametrize("name,slug", [("Acme Inc.", "acme-inc"), ("  --Hello__World--  ", "hello-world"), ("A" * 60, "a" * 40), ("ทดสอบ", None), ("x", None), ("Admin", None)])
def test_slugify(name, slug):
    assert slugify(name) == slug
