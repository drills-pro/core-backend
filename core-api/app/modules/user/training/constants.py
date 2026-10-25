"""Named values for training runs. Status strings mirror the training_runs.status CHECK; they live here
so a switch to a status enum is a change to this file."""

SUBMITTED = "submitted"
RUNNING = "running"
SUCCEEDED = "succeeded"
CANCELLED = "cancelled"

CANCELLABLE = (SUBMITTED, RUNNING)

LOG_TAIL_CHARS = 8000
