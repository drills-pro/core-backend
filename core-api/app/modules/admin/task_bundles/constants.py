from enum import StrEnum

AUTO_PROMOTER = "auto-promote"  # the actor the poller records when it promotes a bundle
DEFAULT_AUTO_PROMOTE = True  # a slug with no task_bundle_settings row; mirrors the column's DEFAULT


class ProblemCode(StrEnum):
    SMOKE_NOT_PASSED = "smoke_not_passed"  # same codes as the runtime packages' promotion gate and smoke retry
    SMOKE_NOT_RETRYABLE = "smoke_not_retryable"
    BUNDLE_IS_STABLE = "bundle_is_stable"  # a slug's stable bundle is replaced by promoting another sha, never retired
