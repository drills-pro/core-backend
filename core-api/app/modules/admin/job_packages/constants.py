from enum import StrEnum

from app.core.enums import JobPackageName
from app.core.job_packages import TASK_PACKAGE_PREFIX

AUTO_PROMOTER = "auto-promote"  # the actor the poller records when it moves a target
SMOKE_NOT_PASSED_DETAIL = "the package's smoke job has not passed"
SMOKE_NOT_RETRYABLE_DETAIL = "only a candidate package whose smoke job failed can be smoked again"
STABLE_NOT_RETIRABLE_DETAIL = "the target of a package is replaced by promoting another version; it cannot be retired"
TARGET_NAME_PATTERN = rf"^({'|'.join(JobPackageName)}|{TASK_PACKAGE_PREFIX}[a-z][a-z0-9-]*)$"  # a runtime package or a task bundle


class ProblemCode(StrEnum):
    STABLE_NOT_RETIRABLE = "stable_not_retirable"
