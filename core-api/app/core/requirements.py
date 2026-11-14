"""Pinned Python requirements of a runtime package or task bundle (planning/26): exact `name==version` pins only.

Ray installs them with its `uv` runtime env on the stock image, so what reaches a job must be reproducible: no ranges, no
URLs, VCS or paths, no option lines (`--index-url`), no environment markers. Pure helpers, no I/O.
"""
import hashlib
import re
from typing import Annotated

from pydantic import AfterValidator

MAX_REQUIREMENTS = 500  # pins in one list; a CUDA torch environment is about 100
MAX_REQUIREMENT_LENGTH = 200  # characters in one pin, normalised
_SEPARATORS = re.compile(r"[-_.]+")  # PEP 503: runs of these in a name are one
_NAME = r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?"
_VERSION = (  # PEP 440 without wildcards: epoch, release, pre, post, dev and a local part (2.11.0+cu128)
    r"(?:\d+!)?\d+(?:\.\d+)*(?:[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview)[-_.]?\d*)?"
    r"(?:-\d+|[-_.]?(?:post|rev|r)[-_.]?\d*)?(?:[-_.]?dev[-_.]?\d*)?(?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?"
)
_PIN = re.compile(rf"(?P<name>{_NAME})(?:\[(?P<extras>{_NAME}(?:,{_NAME})*)\])?==(?P<version>{_VERSION})", re.IGNORECASE)


class RequirementsError(ValueError):
    """A requirement list that is not exact pins, or that conflicts with itself or another list."""


def _normalise_one(raw: str) -> tuple[str, str]:
    """(normalised name, normalised pin) of one requirement."""
    if len(raw) > MAX_REQUIREMENT_LENGTH:
        raise RequirementsError(f"a requirement is at most {MAX_REQUIREMENT_LENGTH} characters")
    match = _PIN.fullmatch(raw)
    if match is None:
        raise RequirementsError(f"{raw!r} is not an exact pin (name==version, no ranges, URLs, options or markers)")
    name = _SEPARATORS.sub("-", match["name"].lower())
    extras = ",".join(sorted({_SEPARATORS.sub("-", e.lower()) for e in (match["extras"] or "").split(",") if e}))
    return name, f"{name}{f'[{extras}]' if extras else ''}=={match['version'].lower()}"


def normalise_requirements(requirements: list[str]) -> list[str]:
    """Each pin lower-cased and PEP 503 normalised, in the order given. Raises RequirementsError on anything that is not
    an exact pin, on more than MAX_REQUIREMENTS, and on the same package twice."""
    if len(requirements) > MAX_REQUIREMENTS:
        raise RequirementsError(f"at most {MAX_REQUIREMENTS} requirements")
    pins: dict[str, str] = {}
    for raw in requirements:
        name, pin = _normalise_one(raw)
        if name in pins:
            raise RequirementsError(f"{name} is listed twice")
        pins[name] = pin
    return list(pins.values())


def merge_requirements(*lists: list[str]) -> list[str]:
    """The union of already valid lists, sorted. The same pin in two lists merges; two different pins for one package
    (version or extras) raise RequirementsError."""
    merged: dict[str, str] = {}
    for requirements in lists:
        for raw in requirements:
            name, pin = _normalise_one(raw)
            if merged.setdefault(name, pin) != pin:
                raise RequirementsError(f"{name} is pinned twice: {merged[name]} and {pin}")
    return sorted(merged.values())


def requirements_sha256(requirements: list[str]) -> str:
    """Hash of the sorted normalised list, so the order a lock file lists them in does not matter."""
    return hashlib.sha256("\n".join(sorted(normalise_requirements(requirements))).encode()).hexdigest()


Requirements = Annotated[list[str], AfterValidator(normalise_requirements)]  # a pydantic field of exact pins, stored normalised
