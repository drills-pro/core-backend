# Observability — LGTM stack + cost tracking

## Stack
| Signal | Tool | Notes |
|---|---|---|
| Logs | Loki | Scrape from all pods via Grafana Alloy (or Promtail); label by `tenant_id`, `project_id`, `service` |
| Metrics | Mimir (or plain Prometheus if scale doesn't yet justify Mimir's remote-write/HA) | Start with Prometheus + long-term storage via Mimir only once retention/cardinality demands it |
| Traces | Tempo | Instrument app backend + Ray Serve inference path at minimum; training jobs are lower priority for tracing |
| Dashboards | Grafana | Two audiences: internal ops dashboards (cluster health, queue depth) and customer-facing usage dashboards (fed from the same metrics but a separate, simplified view — not raw Grafana exposed to customers) |
| Cost (internal margin) | OpenCost, reading K8s resource requests + Prometheus | Allocates estimated cloud spend by pod labels (`tenant_id`, `job_type`) for internal margin visibility. Not the billing source of truth — see `09-usage-metering.md` for what customers are actually charged on |

## Requirements
- FR1: Every pod/job in the cluster carries `tenant_id` and `project_id` labels — this is the single label convention every other observability requirement depends on. Enforce at job-submission time (Ray/K8s job template), not left to convention.
- FR2: Alerting (Grafana Alertmanager) on: GPU node pool exhaustion, training job failure rate above threshold, inference p99 latency above SLA per deployment, Postgres/Loki/Mimir disk pressure.
- FR3: OpenCost numbers must reconcile with actual cloud billing within an acceptable margin (define: ±5%) — verify against the cloud bill monthly. OpenCost is for internal margin tracking only; customer invoices are computed from `usage_events` per `09-usage-metering.md`, not from OpenCost.
- FR4: Training job logs (Ludwig/Ultralytics stdout, including its own progress/metrics) surfaced to the customer through the UI job-status view — reuse Loki as the source, don't build a separate log pipe for user-facing logs.
- NFR1: Retention: 30 days for logs/traces by default, longer retention as a paid tier if customers want audit history beyond that.
- NFR2: Observability stack must not become a second thing requiring a dedicated ops specialist — Grafana's LGTM stack is chosen specifically because it's one coherent product surface (single Helm chart family, one UI) rather than assembling Prometheus + ELK + Jaeger separately.

## Open questions
- Whether customer-facing dashboards embed real Grafana panels (via Grafana's public/embed sharing) vs a fully custom UI reading the same data. Embedding is faster to ship but leaks Grafana's UI/branding into a "no-code, doesn't know what Grafana is" product — likely custom UI reading from the same Prometheus/Postgres sources, TBD.
