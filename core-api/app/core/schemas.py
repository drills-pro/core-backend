"""Pydantic types shared by more than one module."""
import re
from typing import Annotated

from pydantic import AfterValidator, StringConstraints, TypeAdapter, ValidationError

TENANT_NAME_MAX_LENGTH = 100
TENANT_SLUG_MIN_LENGTH = 3
TENANT_SLUG_MAX_LENGTH = 40
TENANT_SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"  # mirrors tenants_slug_format
# Handles that would collide with a route or a name the platform itself uses.
RESERVED_SLUGS = frozenset({"admin", "api", "app", "assets", "auth", "billing", "docs", "drills", "help", "login", "new", "settings", "static", "status", "support", "www"})

TenantName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TENANT_NAME_MAX_LENGTH)]


def _not_reserved(slug: str) -> str:
    if slug in RESERVED_SLUGS:
        raise ValueError("this slug is reserved")
    return slug


TenantSlug = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=TENANT_SLUG_MIN_LENGTH, max_length=TENANT_SLUG_MAX_LENGTH, pattern=TENANT_SLUG_PATTERN),
    AfterValidator(_not_reserved),
]
_slug_adapter = TypeAdapter(TenantSlug)


def slugify(name: str) -> str | None:
    """The slug a tenant name suggests, or None when it yields no valid one (e.g. a name with no ASCII letters)."""
    try:
        return _slug_adapter.validate_python(re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:TENANT_SLUG_MAX_LENGTH].strip("-"))
    except ValidationError:
        return None
