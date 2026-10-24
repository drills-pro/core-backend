import pytest

from ray_jobs.common.constants import TABULAR_CLASSIFICATION, TABULAR_REGRESSION
from ray_jobs.common.enums import Code
from ray_jobs.sources.csv_table import infer_type
from tests.support import LIMITS, codes, table


def test_csv_schema_inference_and_split_tag():
    t, rep = table("a,b,c,y\n1,1.5,x,yes\n2,2,y,no\n,3,z,yes\n", split="train")
    assert not rep.failed
    assert t.schema == {"columns": [{"name": "a", "type": "int64"}, {"name": "b", "type": "float64"},
                                    {"name": "c", "type": "string"}, {"name": "y", "type": "string"}]}
    assert t.rows[2]["a"] is None and t.rows[0]["split"] == "train" and t.rows[0]["b"] == 1.5


def test_csv_bool_and_int_columns():
    assert infer_type(["true", "False", ""]) == "bool"
    assert infer_type(["1", "2"]) == "int64" and infer_type(["1", "2.5"]) == "float64" and infer_type([""]) == "string"


@pytest.mark.parametrize("text,code", [
    ("", Code.CSV_NO_HEADER),
    ("a,,y\n1,2,3\n4,5,6\n", Code.CSV_NO_HEADER),
    ("a,a,y\n1,2,3\n4,5,6\n", Code.CSV_DUPLICATE_COLUMNS),
    ("a,split,y\n1,2,3\n4,5,6\n", Code.CSV_DUPLICATE_COLUMNS),
    ("a,b,c,d,e,y\n" + "1,1,1,1,1,1\n" * 3, Code.CSV_TOO_MANY_COLUMNS),
    ("a,y\n1,1\n", Code.CSV_TOO_FEW_ROWS),
    ("a,y\n" + "1,1\n" * 101, Code.CSV_TOO_MANY_ROWS),
    ("a,z\n1,1\n2,2\n", Code.TARGET_MISSING),
    ("a,y\n1,1\n2,\n", Code.TARGET_MISSING),
    ("a,y\n1,1.5\n2,2.5\n", Code.TARGET_TYPE_MISMATCH),  # float target cannot be a class
    ("a,y\n1,x\n2,z,extra\n", Code.CSV_ROW_TOO_LONG),  # never truncated
])
def test_csv_errors(text, code):
    t, rep = table(text)
    assert t is None and codes(rep) == [code]


def test_regression_target_must_be_numeric():
    t, rep = table("a,y\n1,x\n2,z\n", task=TABULAR_REGRESSION)
    assert t is None and codes(rep) == [Code.TARGET_TYPE_MISMATCH]
    t, rep = table("a,y\n1,1.5\n2,2\n", task=TABULAR_REGRESSION)
    assert t is not None and not rep.failed
