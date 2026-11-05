"""A typed table read from a CSV or a Hugging Face data file."""
from dataclasses import dataclass

from ray_jobs.common.enums import ColumnType


@dataclass
class Table:
    columns: list[str]
    types: list[ColumnType]
    rows: list[dict]  # typed values, plus Col.SPLIT

    @property
    def schema(self) -> dict:
        return {"columns": [{"name": n, "type": t} for n, t in zip(self.columns, self.types)]}


@dataclass
class Extracted:
    """What one source produced. `rows` for image tasks, `table` for tabular ones; `target` is the label or target column."""

    rows: list[dict] | None
    target: str | None
    table: Table | None
    revision: str | None = None
    license: list[str] | None = None
