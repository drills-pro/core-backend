"""Pydantic types shared by more than one module."""
from typing import Annotated

from pydantic import StringConstraints

TENANT_NAME_MAX_LENGTH = 100

TenantName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TENANT_NAME_MAX_LENGTH)]
