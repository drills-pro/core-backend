import pytest
from pydantic import BaseModel, ValidationError

from app.core.requirements import (
    MAX_REQUIREMENT_LENGTH,
    MAX_REQUIREMENTS,
    Requirements,
    RequirementsError,
    merge_requirements,
    normalise_requirements,
    requirements_sha256,
)


def test_exact_pins_are_accepted_and_normalised():
    assert normalise_requirements(["Torch==2.11.0+cu128", "ray[Serve]==2.60.0", "Typing_Extensions==4.12.2", "x==1!2.0rc1.post3.dev4"]) == [
        "torch==2.11.0+cu128", "ray[serve]==2.60.0", "typing-extensions==4.12.2", "x==1!2.0rc1.post3.dev4"]


def test_extras_are_sorted():
    assert normalise_requirements(["ray[tune,Serve]==2.60.0"]) == ["ray[serve,tune]==2.60.0"]


@pytest.mark.parametrize("bad", [
    "numpy", "numpy>=1.0", "numpy~=1.0", "numpy==1.*", "numpy===1.0", "numpy!=1.0", "numpy == 1.0", "numpy==1.0 ",
    "numpy @ https://example.com/n.whl", "git+https://example.com/n.git", "./local", "/abs/n.whl", "numpy==1.0; python_version<'3.12'",
    "--index-url https://x", "-r other.txt", "", "# comment", "numpy==", "==1.0", "numpy[==1.0", "numpy[]==1.0"])
def test_anything_but_an_exact_pin_is_refused(bad):
    with pytest.raises(RequirementsError):
        normalise_requirements([bad])


def test_duplicates_are_refused_whatever_the_spelling():
    with pytest.raises(RequirementsError, match="twice"):
        normalise_requirements(["Typing_Extensions==4.0", "typing-extensions==4.0"])


def test_limits():
    assert len(normalise_requirements([f"p{i}==1" for i in range(MAX_REQUIREMENTS)])) == MAX_REQUIREMENTS
    with pytest.raises(RequirementsError):
        normalise_requirements([f"p{i}==1" for i in range(MAX_REQUIREMENTS + 1)])
    with pytest.raises(RequirementsError):
        normalise_requirements(["a" * MAX_REQUIREMENT_LENGTH + "==1"])


def test_sha256_ignores_order_and_spelling():
    assert requirements_sha256(["b==1", "A==2"]) == requirements_sha256(["a==2", "B==1"])
    assert requirements_sha256(["a==2"]) != requirements_sha256(["a==3"])


def test_merge_unions_identical_pins_and_refuses_different_ones():
    assert merge_requirements(["torch==2.11.0", "numpy==2.1.0"], ["Numpy==2.1.0", "xgboost==3.2.0"]) == ["numpy==2.1.0", "torch==2.11.0", "xgboost==3.2.0"]
    assert merge_requirements([], []) == []
    with pytest.raises(RequirementsError, match="numpy"):
        merge_requirements(["numpy==2.1.0"], ["numpy==2.2.0"])


def test_pydantic_type_validates_and_normalises():
    class M(BaseModel):
        r: Requirements = []

    assert M(r=["Numpy==1.0"]).r == ["numpy==1.0"]
    with pytest.raises(ValidationError):
        M(r=["numpy>=1"])
