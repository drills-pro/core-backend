from enum import StrEnum


class ModelStatus(StrEnum):
    """The statuses admin_models() derives from a model's newest deployment (a test checks the function names each)."""

    DEPLOYED = "deployed"
    DEPLOYING = "deploying"
    STOPPED = "stopped"
    NOT_DEPLOYED = "not_deployed"


class ProblemCode(StrEnum):
    INVALID_FILTER = "invalid_filter"


TOTAL_COLUMN = "total"  # count(*) OVER () on every row: the pager's, not part of a row
