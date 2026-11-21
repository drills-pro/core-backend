"""Zip archive safety: path rules, limits, and a budget on bytes really decompressed."""
import zipfile

from ray_jobs.common.constants import APPLEDOUBLE_PREFIX, IGNORED_DIRS, MODE_MASK, READ_CHUNK, SYMLINK_MODE, WINDOWS_DRIVE_RE
from ray_jobs.common.enums import Code
from ray_jobs.common.limits import LimitExceeded, Limits
from ray_jobs.common.report import Report


def is_unsafe_path(name: str) -> bool:
    parts = name.split("/")
    return name.startswith("/") or "\\" in name or bool(WINDOWS_DRIVE_RE.match(name)) or ".." in parts


def is_symlink(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & MODE_MASK == SYMLINK_MODE


def inspect_archive(infos: list[zipfile.ZipInfo], archive_size: int, limits: Limits, report: Report) -> list[zipfile.ZipInfo]:
    """Apply the fixed archive rules; returns the file members worth reading (errors go to the report)."""
    if archive_size > limits.upload_max_bytes:
        report.error(Code.ARCHIVE_TOO_LARGE, f"upload is {archive_size} bytes, limit {limits.upload_max_bytes}")
    members: list[zipfile.ZipInfo] = []
    seen_names: set[str] = set()
    for info in infos:
        name = info.filename
        if is_unsafe_path(name):
            report.error(Code.ARCHIVE_UNSAFE_PATH, "absolute path or '..' in archive member", name)
            continue
        parts = [p for p in name.split("/") if p]
        if IGNORED_DIRS.intersection(parts) or info.is_dir() or (parts and parts[-1].startswith(APPLEDOUBLE_PREFIX)):
            continue
        if is_symlink(info):
            report.error(Code.ARCHIVE_SYMLINK, "symlinks are not allowed", name)
            continue
        if len(name) > limits.max_path_length:
            report.error(Code.PATH_TOO_LONG, f"path longer than {limits.max_path_length}", name)
            continue
        if len(parts) - 1 > limits.max_archive_depth:
            report.error(Code.ARCHIVE_TOO_DEEP, f"deeper than {limits.max_archive_depth} directories", name)
            continue
        if name in seen_names:
            report.error(Code.ARCHIVE_DUPLICATE_PATH, "two archive members share one path", name)
            continue
        seen_names.add(name)
        members.append(info)
    if len(members) > limits.max_files:
        report.error(Code.ARCHIVE_TOO_MANY_FILES, f"{len(members)} files, limit {limits.max_files}")
    total = sum(m.file_size for m in members)
    if total > limits.extracted_max_bytes:
        report.error(Code.ARCHIVE_TOO_LARGE, f"extracted size {total} over limit {limits.extracted_max_bytes}")
    if archive_size and total / archive_size > limits.extracted_max_ratio:
        report.error(Code.ARCHIVE_TOO_LARGE, f"compression ratio over {limits.extracted_max_ratio}: zip bomb")
    return members


class ByteBudget:
    """Counts bytes really decompressed: the sizes in a zip header can lie."""

    def __init__(self, limit: int):
        self.left = limit

    def take(self, n: int, code: Code = Code.ARCHIVE_TOO_LARGE) -> None:
        self.left -= n
        if self.left < 0:
            raise LimitExceeded(code, "data expands past the extracted size limit")

    def read(self, zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
        out = bytearray()
        with zf.open(info) as f:
            while chunk := f.read(READ_CHUNK):
                self.take(len(chunk))
                out += chunk
        return bytes(out)
