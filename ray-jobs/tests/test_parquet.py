"""The real pyarrow paths."""
import io

import pyarrow.parquet as pq
import pytest

from ray_jobs.common.limits import LimitExceeded
from ray_jobs.common.parquet import index_parquet, read_parquet, table_parquet
from ray_jobs.common.rows import image_row
from tests.support import JPG, PNG, table


def test_real_parquet_round_trip_of_an_image_index():
    boxes = [{"category": "c", "x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0}]
    rows = [image_row("a/1.jpg", JPG, "a", None, "train"), image_row("2.png", PNG, None, boxes, None)]
    assert read_parquet(index_parquet(rows)) == rows
    assert read_parquet(index_parquet([])) == []


def test_real_parquet_round_trip_of_a_table_keeps_types_and_all_empty_columns():
    t, _ = table("n,f,b,e,y\n1,1.5,true,,x\n2,,false,,z\n")
    data = table_parquet(t.rows, t)
    assert read_parquet(data) == t.rows
    assert [(f.name, str(f.type)) for f in pq.read_schema(io.BytesIO(data))] == [
        ("n", "int64"), ("f", "double"), ("b", "bool"), ("e", "string"), ("y", "string"), ("split", "string")]


def test_read_parquet_refuses_a_file_that_inflates_past_the_budget():
    t = table("a,y\n" + "1,x\n" * 50)[0]
    data = table_parquet(t.rows, t)
    assert len(read_parquet(data, 10**9)) == 50
    with pytest.raises(LimitExceeded):
        read_parquet(data, 1)
