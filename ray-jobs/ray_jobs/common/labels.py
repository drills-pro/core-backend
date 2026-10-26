"""Label rules shared by every image source and the merge."""
import unicodedata

from ray_jobs.common.enums import Code
from ray_jobs.common.report import Report


def is_safe_label(label: str) -> bool:
    """A label is one path segment: no separators, not '.' or '..'."""
    return "/" not in label and "\\" not in label and label not in (".", "..")


def normalise_label(label: str) -> str:
    return unicodedata.normalize("NFC", label).strip()


def case_collisions(labels) -> list[tuple[str, str]]:
    """Pairs of distinct labels that differ only by case."""
    seen: dict[str, str] = {}
    out = []
    for label in sorted(set(labels)):
        key = label.casefold()
        if key in seen:
            out.append((seen[key], label))
        else:
            seen[key] = label
    return out


def check_label_collisions(labels, report: Report) -> None:
    for a, b in case_collisions(labels):
        report.error(Code.LABEL_CASE_COLLISION, f"labels '{a}' and '{b}' differ only by case", b)
