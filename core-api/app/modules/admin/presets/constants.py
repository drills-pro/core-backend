from enum import StrEnum


class ProblemCode(StrEnum):
    TASK_NOT_TRAINABLE = "task_type_not_trainable"
    INVALID_RECIPE = "invalid_training_recipe"
    MODEL_NOT_ADMITTED = "model_not_admitted"
    HUB_UNAVAILABLE = "hub_unavailable"
