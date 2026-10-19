# SLOs and cost estimate

Status: **deploy-ready; not applied (no cloud account connected).** Nothing here has been measured on
Cloud Run. Latency and quality numbers that *were* measured come from the local benchmark
([`results/benchmark_summary.json`](../results/benchmark_summary.json), local `llama3.1:8b` answer
model on a laptop GPU) and are quoted only as the baseline the cloud targets are set against.

## SLIs and SLOs (per environment; prod targets shown)

| SLI | Definition (how it is measured) | SLO (28-day window) | Baseline we have |
|---|---|---|---|
| Availability | Good = `POST /ask` responses with status < 500, excluding `503` sent while the kill switch is on. Bad = 5xx + Cloud Run `request_count` with `response_code_class="5xx"`. 401/422/429 are client outcomes, not errors. | **99.5%** (error budget: 3 h 22 min per 28 days) | Not measured. Dependency SLAs bound it: Cloud Run 99.95%, Cloud SQL Enterprise 99.95% (HA), AuraDB Professional (no uptime SLA; Business Critical has 99.95%), Groq (no SLA on the free/dev tier). |
| Latency | `run.googleapis.com/request_latencies` for `/ask`, and `latency_ms` per row in the `routing_log` table | **p50 ≤ 5 s, p95 ≤ 12 s, p99 ≤ 20 s** (provisional, set before any cloud measurement) | Local hybrid: p50 8.0 s, p95 15.4 s, dominated by the local 8B answer model. Cloud answers run on Groq `gpt-oss-20b`; not yet measured. |
| Readiness | Fraction of 1-minute probes where `/readyz` returns 200 | **99.5%** | Locally verified to flip to 503 when Postgres stops and recover when it returns; the CI `smoke` job re-checks this on every PR (stop Postgres -> 503 -> start -> 200). |
| Citation validity (quality) | Answered (non-refused) `/ask` responses whose every citation is a retrieved chunk id. Enforced in code, so a miss is a bug. | **100%** | 100% of 128 benchmark answers had >= 1 validated citation. |
| Out-of-scope refusal (quality) | Benchmark out-of-scope questions refused | **100%** on every release | 10/10 (both systems). |
| Answer accuracy (quality, offline) | `scripts/run_benchmark.py` on the 64-question set before changing models, prompts or retrieval | Hybrid "All" accuracy **>= 65%** and multi-hop **>= 58%** (published 70% / 63% minus 5 pts for n = 64 noise) | 70% all, 63% multi-hop with local llama3.1:8b. Must be re-run with the cloud answer model before relying on these gates. |
| Graceful degradation | Share of hybrid `/ask` rows in `routing_log` with `fallback_reason LIKE 'router_error%'` | < 10% per day (alert, not an SLO) | 34/64 in the benchmark run, caused by the shared free-tier Groq quota. |

Error budget policy: if the 28-day availability budget is spent, freeze feature deploys (CD still
ships fixes), and look first at Groq rate limits and AuraDB, the two dependencies without SLAs.

### Monitoring as code

`infra/terraform/modules/kgrag/monitoring.tf` provisions, per environment (validated against
hashicorp/google 8.7.0, **not applied**):

- an uptime check on `/healthz` (60 s, OIDC-authenticated) with an alert when it fails from 2+ regions;
- the availability SLO above as a `google_monitoring_slo` (request-based: 5xx vs all requests,
  28-day rolling, goal `slo_availability_goal` = 0.995) and **multi-window burn-rate alerts**: fast
  (14.4x over 1 h AND 5 min) and slow (6x over 6 h AND 30 min), following the Google SRE workbook;
- golden-signal alerts: 5xx ratio > 5% (5 min), p95 latency > 12 s (10 min, `slo_latency_p95_ms`),
  instance saturation (all `max_instances` active, CPU p99 > 85%, memory p99 > 90%);
- Cloud SQL: down, CPU > 80%, disk > 85%, connections > 80;
- app-specific log-based alerts: runtime DB role not least-privilege, LLM spend cap refusals, shared
  rate limiter unavailable, router fallback storms (> 20 in 10 min);
- an email notification channel from `alert_email` (TF_VAR; not committed).

The availability SLI counts every 5xx, including the 503s sent on purpose while the kill switch is on;
Cloud Run's request metrics cannot tell them apart. Treat a kill-switch window as a planned budget
spend. Alert responses are in docs/RUNBOOK.md section 8.

### Verification

`scripts/smoke.py` checks the functional SLIs on every deploy (CD) and every PR (CI compose stack):
citations resolve to retrieved chunk ids, out-of-scope is refused, auth, limits, kill switch,
security headers. See docs/RUNBOOK.md section 3.

## Monthly cost estimate

**Estimate, not a bill.** List prices for `us-central1`, on-demand, retrieved 2026-10-17 from the pages
cited below; verify with the [Google Cloud pricing calculator](https://cloud.google.com/products/calculator)
before committing. 730 hours/month. Free tiers are ignored (they are per billing account and may be
used by other projects), so small items may actually be $0.

### Unit prices used

| Item | Price | Source |
|---|---|---|
| Cloud Run request-based CPU / memory (active) | $0.000024 per vCPU-s, $0.0000025 per GiB-s | [Cloud Run pricing](https://cloud.google.com/run/pricing) |
| Cloud Run min-instance idle CPU / memory | $0.0000025 per vCPU-s, $0.0000025 per GiB-s | same |
| Cloud Run requests | $0.40 per million (2 M free) | same |
| Cloud SQL Enterprise vCPU / memory (zonal) | $0.0413 per vCPU-h, $0.007 per GiB-h | [Cloud SQL pricing](https://cloud.google.com/sql/pricing) |
| Cloud SQL Enterprise vCPU / memory (HA) | $0.0826 per vCPU-h, $0.014 per GiB-h | same |
| Cloud SQL SSD storage | $0.17 per GB-month zonal, ~$0.34 HA; backups ~$0.08 per GB-month | same |
| Secret Manager | $0.06 per active version per month (6 free); $0.03-0.06 per 10k accesses | [Secret Manager pricing](https://cloud.google.com/secret-manager/pricing) |
| Artifact Registry | $0.10 per GB-month after 0.5 GB | [Artifact Registry pricing](https://cloud.google.com/artifact-registry/pricing) |
| Neo4j AuraDB Professional (external) | $65.70 per month for 1 GB / 1 CPU / 2 GB storage | [Neo4j pricing](https://neo4j.com/pricing) |
| Groq `gpt-oss-20b` (router + answers) | $0.075 per M input, $0.30 per M output | [`src/kgrag/pricing.json`](../src/kgrag/pricing.json) (estimate, verify at groq.com/pricing) |

### Prod (terraform.tfvars: min 1 / max 5 instances, 1 vCPU / 2 GiB, Cloud SQL `db-custom-2-7680` HA)

Traffic assumption: **100,000 `/ask` per month**, 3 s billed per request with no overlap (an upper
bound for request-based billing; 3 s is an assumption, Groq latency on Cloud Run is not measured).

| Driver | Calculation | USD/month |
|---|---|---:|
| Cloud SQL compute (HA) | 2 vCPU x $0.0826 x 730 + 7.5 GiB x $0.014 x 730 | 197.25 |
| Cloud SQL storage + backups | 10 GB x $0.34 + ~2 GB backups x $0.08 | 3.56 |
| Neo4j AuraDB Professional 1 GB | list price (graph is ~500 nodes / ~1,000 relationships; 1 GB is the minimum) | 65.70 |
| Cloud Run warm instance (idle floor) | 2,628,000 s x (1 x $0.0000025 + 2 x $0.0000025) | 19.71 |
| Cloud Run active time | 300,000 s x ($0.000024 + 2 x $0.0000025) | 8.70 |
| Groq LLM tokens | 100k x (3,042 in x $0.075 + 220 out x $0.30) / 1M; tokens/query = measured mean of the hybrid benchmark rows (router + answer calls, llama tokenizer) | 29.42 |
| Secret Manager, Artifact Registry, logging | 7 versions x $0.06; ~5 GB images x $0.10; logs inside the free 50 GiB | ~1 |
| **Total** | | **~325** |

### Staging (min 0 / max 2, Cloud SQL `db-custom-1-3840` zonal)

| Driver | Calculation | USD/month |
|---|---|---:|
| Cloud SQL compute (zonal) | 1 x $0.0413 x 730 + 3.75 x $0.007 x 730 | 49.31 |
| Cloud SQL storage | 10 GB x $0.17 | 1.70 |
| Neo4j AuraDB Professional 1 GB | AuraDB Free pauses when idle, which fails `/readyz` | 65.70 |
| Cloud Run | scales to zero; smoke tests make no LLM calls | ~0 |
| Groq | manual testing | ~1 |
| **Total** | | **~118** |

Cost drivers, in order: the always-on databases (Cloud SQL HA and AuraDB, ~82% of prod) dominate at
this traffic; LLM tokens scale linearly with queries (~$0.0003 per hybrid query); Cloud Run is small
because the service is idle between requests. Levers: Cloud SQL zonal in prod (halves compute, loses
the HA standby), one Cloud SQL instance with two databases for staging and prod, committed-use
discounts, and `min_instances = 0` in prod if a cold start (model + catalog load) is acceptable.

LLM spend is billed by Groq, not GCP, so GCP budgets do not cap it. In code the cap is the shared
`llm_spend_ledger` table in Cloud SQL (`LLM_SPEND_CAP_USD`, checked and reserved in one transaction by
every instance and job); see the residual risks in the README.
