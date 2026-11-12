from pydantic import BaseModel

from app.core.enums import TaskType


class CatalogModel(BaseModel):
    repo_id: str
    revision: str
    license: str | None
    tasks: list[TaskType]
    presets: list[str]
