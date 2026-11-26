from enum import StrEnum

DEFAULT_AUTO_PROMOTE = True  # a slug with no task_bundle_settings row; mirrors the column's DEFAULT
SMOKE_NOT_PASSED_DETAIL = "the bundle's smoke job has not passed"
SMOKE_NOT_RETRYABLE_DETAIL = "only a candidate whose smoke failed can be smoked again"
BUNDLE_IS_STABLE_DETAIL = "the stable bundle of a task is replaced by promoting another sha, never retired"


class ProblemCode(StrEnum):
    BUNDLE_IS_STABLE = "bundle_is_stable"  # a slug's stable bundle is replaced by promoting another sha, never retired
