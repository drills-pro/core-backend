from app.core.metrics import base


def test_names_must_nest_under_the_service_prefix():
    assert base.NAME_PATTERN.match("drills.core_backend.a.b.c_d")
    for bad in ("drills.core_backend.x", "core_backend.a.b", "drills.core-backend.a.b", "drills.core_backend.A.b"):
        assert not base.NAME_PATTERN.match(bad)
