from enum import StrEnum

AUTO_PROMOTER = "auto-promote"  # the actor the poller records when it promotes a bundle
DEFAULT_AUTO_PROMOTE = True  # a slug with no task_bundle_settings row; mirrors the column's DEFAULT
SMOKE_NOT_PASSED_DETAIL = "the bundle's smoke job has not passed"  # the codes are the data plane's, shared with the runtime packages
SMOKE_NOT_RETRYABLE_DETAIL = "only a candidate whose smoke failed can be smoked again"


class ProblemCode(StrEnum):
    BUNDLE_IS_STABLE = "bundle_is_stable"  # a slug's stable bundle is replaced by promoting another sha, never retired
