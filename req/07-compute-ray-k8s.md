# Compute — Ray + Kubernetes

## Cluster shape
Single K8s cluster (EKS/GKE), KubeRay operator managing Ray clusters for both training (Ray Train/Tune) and serving (Ray Serve). See `00-overview.md` for phasing — this is the phase 1+ target, not phase 0.

## Node pools
| Pool | Purpose | Instance type | Autoscaling |
|---|---|---|---|
| System | App backend, Postgres (or managed RDS), ingress, Zitadel, MLflow, Label Studio | CPU, on-demand | Cluster Autoscaler, min 1 |
| Training | Ray Train/Tune jobs (Ludwig, Ultralytics) | GPU, spot-preferred | Karpenter, scale-to-zero, checkpointing required for spot preemption |
| Serving | Ray Serve replicas | GPU or CPU depending on model, on-demand for latency-sensitive tenants | Karpenter, min replicas per active deployment |

## Requirements
- FR1: Training jobs submitted via internal service with `tenant_id`/`project_id` labels propagated to the underlying K8s pod labels (required for OpenCost allocation, see 08).
- FR2: Training jobs on spot nodes checkpoint to object storage at a configurable interval; job resumes from last checkpoint on preemption without manual intervention.
- FR3: Ray Serve deployments support multiplexing multiple tenant models onto shared GPU replicas for low-traffic tenants (see prior BentoML-vs-Ray-Serve discussion) — this is the core unit-economics requirement, not optional.
- FR4: Hard resource quotas per tenant (max concurrent training jobs, max GPU-hours/month on a given plan tier) enforced at job submission, not just billed after the fact.
- NFR1: Training job queue depth and wait time visible to the platform team (not necessarily the customer) — needed to catch capacity problems before customers do.
- NFR2: No tenant's training job may preempt another tenant's serving replica. Separate node pools (above) plus K8s priority classes enforce this.

## Explicitly deferred to phase 2
Argo Workflows for multi-step pipeline DAGs (ingest → preprocess → train → eval → register → deploy) — phase 1 runs this as a single Ray job with sequential steps in code; introduce Argo only once pipeline branching/retry logic outgrows that.

## Open questions
- Multi-region — not addressed here. Single-region deployment assumed for v1; revisit if a customer has data-residency requirements.
