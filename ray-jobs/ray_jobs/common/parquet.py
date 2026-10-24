"""Parquet encoding of index.parquet and data.parquet. pyarrow is imported lazily."""
import io

from ray_jobs.common.enums import BOX_FIELDS, INDEX_COLUMNS, Box, Col, Code, ColumnType
from ray_jobs.common.limits import LimitExceeded
from ray_jobs.common.table import Table


def _parquet(table) -> bytes:
    import pyarrow.parquet as pq

    sink = io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


def index_parquet(rows: list[dict]) -> bytes:
    import pyarrow as pa

    box = pa.struct([(f, pa.string() if f == Box.CATEGORY else pa.float64()) for f in BOX_FIELDS])
    types = {Col.SIZE: pa.int64(), Col.BOXES: pa.list_(box)}
    schema = pa.schema([(c.value, types.get(c, pa.string())) for c in INDEX_COLUMNS])
    return _parquet(pa.Table.from_pylist(rows, schema=schema))


def table_parquet(rows: list[dict], table: Table) -> bytes:
    """Explicit Arrow types from the inferred schema: an all-empty column must stay `string`, not Arrow `null`."""
    import pyarrow as pa

    arrow = {ColumnType.BOOL: pa.bool_(), ColumnType.INT64: pa.int64(),
             ColumnType.FLOAT64: pa.float64(), ColumnType.STRING: pa.string()}
    schema = pa.schema([*[(n, arrow[t]) for n, t in zip(table.columns, table.types)], (Col.SPLIT.value, pa.string())])
    columns = [f.name for f in schema]
    return _parquet(pa.Table.from_pylist([{c: r.get(c) for c in columns} for r in rows], schema=schema))


def read_parquet(data: bytes, max_bytes: int | None = None) -> list[dict]:
    """Rows of a parquet file; with max_bytes, refuse one whose footer declares more uncompressed data."""
    import pyarrow.parquet as pq

    if max_bytes is not None:
        meta = pq.ParquetFile(io.BytesIO(data)).metadata
        if sum(meta.row_group(i).total_byte_size for i in range(meta.num_row_groups)) > max_bytes:
            raise LimitExceeded(Code.ARCHIVE_TOO_LARGE, f"parquet expands past {max_bytes} bytes")
    return pq.read_table(io.BytesIO(data)).to_pylist()
