"""report.json: issues collected during a run (schema: core-api app/integrations/data_plane/report.schema.json)."""
from ray_jobs.common.constants import MAX_REPORT_ISSUES, REPORT_SCHEMA_VERSION
from ray_jobs.common.enums import Code, Outcome, Severity


class Report:
    def __init__(self) -> None:
        self.issues: list[dict] = []
        self.omitted = 0
        self.errors = 0

    def add(self, code: Code, severity: Severity, message: str, path: str | None = None) -> None:
        if severity == Severity.ERROR:
            self.errors += 1
        if len(self.issues) >= MAX_REPORT_ISSUES:
            self.omitted += 1
            return
        self.issues.append({"code": code.value, "severity": severity.value, "path": path, "message": message})

    def error(self, code: Code, message: str, path: str | None = None) -> None:
        self.add(code, Severity.ERROR, message, path)

    def warn(self, code: Code, message: str, path: str | None = None) -> None:
        self.add(code, Severity.WARNING, message, path)

    @property
    def failed(self) -> bool:
        return self.errors > 0

    def to_dict(self, stats: dict | None = None, revision: str | None = None, license: str | None = None) -> dict:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "outcome": Outcome.FAILED if self.failed else Outcome.OK,
            "stats": None if self.failed else stats,
            "issues": self.issues,
            "issues_omitted": self.omitted,
            "revision": revision,
            "license": license,
        }
