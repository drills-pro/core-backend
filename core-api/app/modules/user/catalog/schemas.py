import uuid

from pydantic import BaseModel


class CatalogModel(BaseModel):
    """One enabled catalog entry: a base model pinned by commit, with the licence it was admitted under."""

    id: uuid.UUID
    repo_id: str
    revision: str
    display_name: str
    task: str
    license: str
    params: int | None
    size_bytes: int | None
