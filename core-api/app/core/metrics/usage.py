"""drills.core_backend.<projects|datasets|deployments|training>.*: product events, counted where the write
happens so replicas sum correctly. "Who is active" is a Postgres query, not a metric."""
from app.core.metrics.base import counter

projects_created = counter("projects.created", "Projects created, by task_type (enums.TaskType)")
datasets_published = counter("datasets.published", "Datasets published (a re-publish counts again)")
deployments_created = counter(
    "deployments.created", "New deployments; a 'main' re-deploy updates in place and is not counted"
)
training_runs = counter("training.runs", "Training runs by terminal status (req/08 FR2 failure-rate alert)")
