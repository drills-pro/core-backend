import zipfile

import pytest

from ray_jobs.common.enums import Code
from ray_jobs.common.limits import LimitExceeded, Limits
from ray_jobs.common.report import Report
from ray_jobs.sources.archive import ByteBudget, inspect_archive
from tests.support import LIMITS, codes, make_zip


def test_zip_bomb_by_ratio_and_by_size():
    zf = make_zip({"a/1.jpg": b"\0" * 5000})
    rep = Report()
    inspect_archive(zf.infolist(), 20, LIMITS, rep)  # 5000 / 20 = 250 > 100
    assert Code.ARCHIVE_TOO_LARGE in codes(rep)
    rep = Report()
    inspect_archive(zf.infolist(), 10_000, Limits.from_dict({**LIMITS.__dict__, "extracted_max_bytes": 100}), rep)
    assert Code.ARCHIVE_TOO_LARGE in codes(rep)


def test_decompression_is_bounded_even_if_the_header_lies():
    zf = make_zip({"a/1.jpg": b"\0" * 5000})
    with pytest.raises(LimitExceeded):
        ByteBudget(100).read(zf, zf.infolist()[0])


def test_byte_budget_take_raises_past_the_limit():
    budget = ByteBudget(10)
    budget.take(10)
    with pytest.raises(LimitExceeded):
        budget.take(1)


@pytest.mark.parametrize("name", ["../evil.jpg", "/abs/x.jpg", "a/../../x.jpg", "C:/x.jpg", "a\\b.jpg"])
def test_path_traversal_is_rejected(name):
    rep = Report()
    assert inspect_archive([zipfile.ZipInfo(name)], 100, LIMITS, rep) == []
    assert codes(rep) == [Code.ARCHIVE_UNSAFE_PATH]


def test_symlink_is_rejected():
    zf, rep = make_zip({"cat/1.jpg": b"x"}, symlinks=("cat/link.jpg",)), Report()
    inspect_archive(zf.infolist(), 100, LIMITS, rep)
    assert Code.ARCHIVE_SYMLINK in codes(rep) and rep.failed


def test_depth_path_length_and_file_count_limits():
    rep = Report()
    deep = zipfile.ZipInfo("a/b/c/d/e/f.jpg")
    long = zipfile.ZipInfo("a/" + "x" * 80 + ".jpg")
    inspect_archive([deep, long], 100, LIMITS, rep)
    assert set(codes(rep)) == {Code.ARCHIVE_TOO_DEEP, Code.PATH_TOO_LONG}
    rep = Report()
    inspect_archive([zipfile.ZipInfo(f"a/{i}.jpg") for i in range(51)], 100, LIMITS, rep)
    assert Code.ARCHIVE_TOO_MANY_FILES in codes(rep)


def test_duplicate_archive_members_are_an_error():
    zf, rep = make_zip({"cat/1.jpg": b"x"}), Report()
    assert len(inspect_archive(zf.infolist() * 2, 100, LIMITS, rep)) == 1 and codes(rep) == [Code.ARCHIVE_DUPLICATE_PATH]
