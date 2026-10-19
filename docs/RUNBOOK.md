# Runbook: hopcite on Google Cloud

Status: **deploy-ready; not applied (no cloud account connected).** Every command below is written
for this repository's Terraform and workflows but has not been run against a real project. Treat the
first deploy as a rehearsal in a scratch project.

## Topology

| Piece | Where | Notes |
|---|---|---|
| API (`POST /ask`, `/healthz`, `/readyz`) | Cloud Run service `kgrag-<env>-api` | Gen2, request-based billing, Direct VPC egress (private ranges only) |
| Schema migrations | Cloud Run Job `kgrag-<env>-migrate` | `python -m kgrag.migrate up`, run by CD before every deploy |
| Ingestion | Cloud Run Job `kgrag-<env>-ingest` | `python -m kgrag.jobs ingest` (migrate, fetch EDGAR, build from the committed extraction cache) |
| Vectors + routing log | Cloud SQL for PostgreSQL 17 `kgrag-<env>-pg` | Private IP only, TLS required, PITR, pgvector HNSW |
| Graph | Neo4j AuraDB (external; GCP has no managed Neo4j) | `neo4j+s://` URI, user, password in Secret Manager |
| Secrets | Secret Manager `kgrag-<env>-*` | Containers from Terraform; values added out of band |
| Images | Artifact Registry `kgrag` (created by staging) | Immutable tags; deployed by digest |
| LLMs | Groq (router `gpt-oss-20b` + fallback; answers `gpt-oss-20b`) | Ollama is not available on Cloud Run |

Environment variables used below:

```bash
export PROJECT_ID=<your-project> REGION=us-central1 ENV=staging   # or prod
export SVC=kgrag-${ENV}-api
```

## 1. One-time bootstrap (per GCP project)

Owner/admin credentials on your workstation (`gcloud auth login`, `gcloud auth application-default login`).

```bash
gcloud config set project "$PROJECT_ID"
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  cloudresourcemanager.googleapis.com serviceusage.googleapis.com

# Terraform state bucket (versioned, no public access)
gcloud storage buckets create "gs://${PROJECT_ID}-tfstate" --location="$REGION" \
  --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update "gs://${PROJECT_ID}-tfstate" --versioning

# Keyless GitHub Actions auth: Workload Identity Federation, limited to this repo's main branch
gcloud iam workload-identity-pools create github --location=global --display-name="GitHub Actions"
gcloud iam workload-identity-pools providers create-oidc hopcite \
  --location=global --workload-identity-pool=github \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository=='niravpatidar37/hopcite' && assertion.ref=='refs/heads/main'"

gcloud iam service-accounts create kgrag-deployer --display-name="hopcite GitHub deployer"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud iam service-accounts add-iam-policy-binding "kgrag-deployer@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/niravpatidar37/hopcite"
```

Repository variables (not secrets; nothing secret is stored in GitHub) and the approval gate:

```bash
gh variable set GCP_PROJECT_ID   --body "$PROJECT_ID"
gh variable set GCP_REGION       --body "$REGION"
gh variable set GCP_WIF_PROVIDER --body "projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/providers/hopcite"
gh variable set GCP_DEPLOY_SA    --body "kgrag-deployer@${PROJECT_ID}.iam.gserviceaccount.com"
# Create the `production` environment with yourself as required reviewer (manual approval for prod):
gh api -X PUT repos/niravpatidar37/hopcite/environments/production \
  -F "reviewers[][type]=User" -F "reviewers[][id]=$(gh api user -q .id)"
```

Setting `GCP_PROJECT_ID` is what turns the deploy jobs on. Leave it unset until step 2 is done, or the
first push to `main` fails at `gcloud run jobs update`.

## 2. First deploy (two Terraform phases)

Neo4j: create an AuraDB **Professional** instance on GCP in or near `$REGION` (AuraDB Free pauses when
idle, which fails `/readyz`). Keep its URI, user and password for step 2b.

**2a. Infrastructure without Cloud Run** (network, Cloud SQL, users, secrets, registry, service accounts):

```bash
cd infra/terraform/envs/$ENV
terraform init -backend-config="bucket=${PROJECT_ID}-tfstate" -backend-config="prefix=kgrag/${ENV}"
export TF_VAR_project_id=$PROJECT_ID TF_VAR_region=$REGION \
       TF_VAR_deployer_service_account="kgrag-deployer@${PROJECT_ID}.iam.gserviceaccount.com" \
       TF_VAR_sec_user_agent="hopcite you@yourdomain.com"
terraform plan -out=phase1.tfplan && terraform apply phase1.tfplan
```

Apply **staging first**: it creates the shared Artifact Registry repository that prod reuses.

**2b. Secret values** (the two database passwords were already generated and stored by Terraform):

```bash
new_key() { python -c 'import secrets; print(secrets.token_urlsafe(32))'; }
new_key | tr -d '\n' | gcloud secrets versions add kgrag-${ENV}-api-key --data-file=-
printf '%s' 'neo4j+s://<id>.databases.neo4j.io' | gcloud secrets versions add kgrag-${ENV}-neo4j-uri --data-file=-
printf '%s' 'neo4j'                             | gcloud secrets versions add kgrag-${ENV}-neo4j-user --data-file=-
read -rs NEO4J_PW && printf '%s' "$NEO4J_PW"    | gcloud secrets versions add kgrag-${ENV}-neo4j-password --data-file=- && unset NEO4J_PW
read -rs GROQ_KEY && printf '%s' "$GROQ_KEY"    | gcloud secrets versions add kgrag-${ENV}-groq-api-key --data-file=- && unset GROQ_KEY
```

Hand the API key to clients through your own channel; read it back with
`gcloud secrets versions access latest --secret=kgrag-${ENV}-api-key` (it is not printed anywhere else).

**2c. First image** (CD takes over afterwards):

```bash
REG=${REGION}-docker.pkg.dev/${PROJECT_ID}/kgrag/hopcite
gcloud auth configure-docker "${REGION}-docker.pkg.dev"
docker build -t "${REG}:bootstrap-$(git rev-parse --short HEAD)" . && docker push "${REG}:bootstrap-$(git rev-parse --short HEAD)"
IMAGE=$(docker inspect --format '{{index .RepoDigests 0}}' "${REG}:bootstrap-$(git rev-parse --short HEAD)")
```

**2d. Cloud Run service and jobs**, then schema and data:

```bash
terraform plan -var deploy_services=true -var "image=${IMAGE}" -out=phase2.tfplan && terraform apply phase2.tfplan
gcloud run jobs execute kgrag-${ENV}-migrate --region "$REGION" --wait
gcloud run jobs execute kgrag-${ENV}-ingest  --region "$REGION" --wait   # ~5-10 min, $0 LLM (cached extraction)
```

Keep `-var deploy_services=true` (or set it in `terraform.tfvars`) on every later apply, or Terraform
plans to delete the service.

**2e. Least-privilege check for the runtime DB user (required before the first CD run).** Cloud SQL
adds every API-created user to `cloudsqlsuperuser`, which makes the grants in `R__grants.sql`
meaningless. `R__grants.sql` tries to revoke it, but `kgrag_owner` usually lacks the right, so it only
warns. The problem is reported in three places:

- `python -m kgrag.migrate roles` exits 3 and logs `db role not least-privilege` (the migrate job
  runs the same check after every `up`; `migrate status` exits 3 too);
- `GET /readyz` returns `"db_least_privilege": false` (reported, not gating);
- the log-based alert `kgrag-<env>: db-role-privileged` fires (monitoring.tf).

CD's smoke test runs with `--require-least-privilege`, so a deploy **fails and rolls back** until
this is fixed (set the repo variable `SMOKE_ALLOW_PRIVILEGED_DB_ROLE=true` only as a documented,
temporary exception). Fix, once per environment, as the built-in `postgres` user (Cloud SQL Studio in
the console, or psql from a host on the VPC):

```bash
gcloud sql users set-password postgres --instance "kgrag-${ENV}-pg" --prompt-for-password   # if unknown
```

```sql
REVOKE cloudsqlsuperuser FROM kgrag_app;
SELECT pg_has_role('kgrag_app', 'cloudsqlsuperuser', 'MEMBER');        -- must be false
SELECT has_table_privilege('kgrag_app', 'chunks', 'INSERT');           -- must be false
SELECT has_table_privilege('kgrag_app', 'llm_spend_ledger', 'DELETE'); -- must be false
```

Then confirm with the job (no redeploy needed):

```bash
gcloud run jobs execute "kgrag-${ENV}-migrate" --region "$REGION" --wait \
  --args=-m,kgrag.migrate,roles                         # exit 0 = least privilege
```

## 3. Routine deploys (CD)

Merging to `main` runs `.github/workflows/deploy.yml`:

1. Build the image once, push `…/hopcite:<sha>-<attempt>`, resolve the **digest**.
2. The image is signed with keyless cosign (GitHub OIDC identity, Rekor transparency log); staging
   and prod **verify the signature** (`cosign verify --certificate-identity
   https://github.com/niravpatidar37/hopcite/.github/workflows/deploy.yml@refs/heads/main`) before
   they deploy it.
3. Staging: point both jobs at the digest, execute `migrate`, deploy the service, run the smoke test
   (`scripts/smoke.py`, see below). Failure rolls staging back to its previous revision.
4. Prod waits for approval on the `production` environment, then: migrate, deploy a `candidate`
   revision with **0% traffic**, smoke its tag URL, shift `CANARY_PERCENT` (default 10%), smoke every
   30 s for `CANARY_SOAK_SECONDS` (default 300 s), promote to 100%, smoke again. Any failure moves 100%
   back to the revision that was serving before the run. Smoke summaries are uploaded as artifacts.

### The smoke test (`scripts/smoke.py`)

One stdlib-only script, used by CI (against the compose stack with the fake LLM), by CD (staging,
prod candidate, canary soak, after 100%) and by hand. It prints one PASS/FAIL/SKIP line per check,
exits 1 on any FAIL and writes a JSON summary with `--json-out`. Checks: liveness; readiness with both
stores really pinged; DB role least privilege (`--require-least-privilege`); 401 without / with a
wrong key; the key accepted; 422 on bad input; 422 on a 501-char question and 413 on a 64 KB body;
security headers and no stack traces in error bodies; `/ask` returns a schema-valid answer whose
citations all resolve to the response's `retrieved_chunk_ids`; an out-of-scope question is refused;
the route is valid (with `--expect-router-fallback`: the deterministic fallback when no router LLM
is configured); kill switch behaviour (and, with `--kill-switch-base-url`, a second instance with
the switch on refuses `/ask` with 503 while its probes stay green); a burst ends in 429 with
`Retry-After` (`--skip-rate-limit` in the soak loop).

CD uses the **last** key in the `kgrag-<env>-api-key` secret as the smoke key. Keep a dedicated
smoke key in that position (the rate-limit check drains its bucket; buckets are per key, so real
clients are unaffected). Two `/ask` calls per run hit Groq: a few hundredths of a cent.

```bash
SMOKE_API_KEY=... SMOKE_ID_TOKEN="$(gcloud auth print-identity-token)" \
  python3 scripts/smoke.py --base-url "$URL" --require-least-privilege --json-out smoke.json
```

Manual run: Actions -> deploy -> Run workflow (main only; WIF rejects other refs).

Infrastructure changes are **not** applied by CD. Run `terraform plan` / `apply` by hand per
environment; CD owns only the image and traffic (Terraform ignores both).

## 4. Verify

Run the smoke test above first; the commands below are for digging in.

```bash
URL=$(gcloud run services describe "$SVC" --region "$REGION" --format 'value(status.url)')
TOKEN=$(gcloud auth print-identity-token)          # only needed while public_invoker = false
curl -s -H "Authorization: Bearer $TOKEN" "$URL/healthz"
curl -s -H "Authorization: Bearer $TOKEN" "$URL/readyz"          # {"status":"ready","neo4j":true,"postgres":true,...}
KEY=$(gcloud secrets versions access latest --secret=kgrag-${ENV}-api-key | cut -d, -f1)
curl -s -X POST "$URL/ask" -H "Authorization: Bearer $TOKEN" -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' -d '{"question":"Which companies does Supermicro name as competitors?"}'
gcloud run services describe "$SVC" --region "$REGION" --format 'yaml(status.traffic)'
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=$SVC AND severity>=WARNING" --limit 50 --freshness 1h
```

When `public_invoker = false` (staging default), callers need `roles/run.invoker` and an ID token.
`gcloud auth print-identity-token` works for user accounts; for a service account use
`--impersonate-service-account=<sa> --audiences=kgrag-${ENV}-api`.

## 5. Rollback

**Application (seconds, no rebuild).** Move traffic to a known-good revision:

```bash
gcloud run revisions list --service "$SVC" --region "$REGION" --limit 10
gcloud run services update-traffic "$SVC" --region "$REGION" --to-revisions <REVISION>=100
```

This pins traffic. The next CD run re-routes to the latest revision; if you fix forward by hand, run
`gcloud run services update-traffic "$SVC" --region "$REGION" --to-latest` afterwards.

**Jobs.** `gcloud run jobs update kgrag-${ENV}-migrate --image <previous digest> --region "$REGION"`.

**Database schema.** Migrations are forward-only and must be expand-only (add tables/columns/indexes;
never drop or rename in the same release as code that stops using them), so the previous revision
keeps working after a migration. There are no down migrations. To undo data damage, restore Cloud SQL
to a point in time into a new instance and repoint `PG_HOST`:

```bash
gcloud sql instances clone kgrag-${ENV}-pg kgrag-${ENV}-pg-restore --point-in-time "2026-10-17T12:00:00Z"
```

**Graph.** Restore an AuraDB snapshot from the Aura console, or rebuild: ingestion is idempotent MERGE,
`gcloud run jobs execute kgrag-${ENV}-ingest --region "$REGION" --wait`.

**Infrastructure.** `git revert` the Terraform change, then `terraform plan` / `apply` in the env dir.

## 6. Rotate secrets

All secret env vars resolve `latest` **when an instance starts**, so every rotation is: add a version,
then roll a new revision (`gcloud run services update "$SVC" --region "$REGION" --update-env-vars SECRETS_ROTATED_AT=$(date +%s)`
followed by `gcloud run services update-traffic "$SVC" --region "$REGION" --to-latest`), then disable the
old version. Jobs resolve `latest` at each execution start, so they need no update.

| Secret | Procedure |
|---|---|
| `api-key` | Zero downtime: add a version holding `NEW,OLD,SMOKE` (comma-separated, all accepted; the CD smoke key stays last), roll a revision, move clients to NEW, add a version holding `NEW,SMOKE`, roll again, `gcloud secrets versions disable <old> --secret=kgrag-${ENV}-api-key`. |
| `groq-api-key` | Create the new key in the Groq console, add version, roll the revision, revoke the old key at Groq. |
| `neo4j-password` | Aura supports one password per user: change it in Aura, immediately add the version and roll the revision (brief `/readyz` failures on old instances until replaced). |
| `pg-app-password`, `pg-owner-password` | `terraform apply -var db_password_version=<n+1>` writes new random passwords to Cloud SQL and Secret Manager in one apply (values never in state); then roll the service. Old instances fail DB calls until replaced; do it in a quiet window. |

## 7. Kill switch (stop all LLM calls)

`KGRAG_KILL_SWITCH=1` makes `/ask` answer `503` without retrieval or any model call, and makes the LLM
client refuse calls in jobs too. `/healthz` and `/readyz` keep working (`"llm_enabled": false`).

```bash
gcloud run services update "$SVC" --region "$REGION" --update-env-vars KGRAG_KILL_SWITCH=1
gcloud run services update-traffic "$SVC" --region "$REGION" --to-latest   # in case traffic was pinned
gcloud run jobs update kgrag-${ENV}-ingest --region "$REGION" --update-env-vars KGRAG_KILL_SWITCH=1
```

Then set `kill_switch = true` in the env's `terraform.tfvars` (or Terraform reverts it on the next
apply). Undo with `KGRAG_KILL_SWITCH=0` and `kill_switch = false`.

Hard stop that does not wait for a revision: revoke the Groq key in the Groq console. All LLM calls
then fail; the router falls back to deterministic routing and answers become refusals.

## 8. Monitoring and alerts (`infra/terraform/modules/kgrag/monitoring.tf`)

Created by Terraform per environment; notifications go to `TF_VAR_alert_email` (empty = policies
without a channel, visible in the console). Every policy's documentation links back here. Thresholds
are starting points from docs/SLO.md, untested against real traffic.

| Alert | Signal | Fires when | First action |
|---|---|---|---|
| `/healthz uptime check failing` | Uptime check (60 s, OIDC token from the Monitoring service agent) | fails from 2+ regions | section 4; recent deploy? roll back (section 5) |
| `5xx ratio > 5%` | `run.googleapis.com/request_count` 5xx / all, 5 min | ratio > `alert_5xx_ratio` | kill switch on? (503 expected); logs `severity>=ERROR`; Cloud SQL / AuraDB status |
| `p95 latency > 12000 ms` | `request_latencies` p95, 10 min | above `slo_latency_p95_ms` | `routing_log` latency by `final_route`; Groq status; cold starts |
| `instance saturation` | active instances at max, CPU p99 > 85%, memory p99 > 90% (10 min) | any | raise `max_instances` / `cpu` / `memory`, or shed load |
| `error budget burn (fast, 14.4x)` / `(slow, 6x)` | SLO `availability-28d` (99.5%), `select_slo_burn_rate` over 1 h + 5 min / 6 h + 30 min | both windows above the rate | error budget policy (docs/SLO.md): freeze feature deploys |
| `Cloud SQL down or saturated` | `database/up`, CPU > 80%, disk > 85%, backends > 80 | any | section 5 (PITR), raise tier/disk; API pools use 8 connections per instance |
| `db-role-privileged` | log line `db role not least-privilege` | > 0 in 10 min | section 2e |
| `spend-cap-refusals` | log line `llm spend cap reached` | > 0 in 10 min | `SELECT status, sum(usd) FROM llm_spend_ledger GROUP BY 1;` raise `llm_spend_cap_usd` deliberately or wait |
| `rate-limiter-unavailable` | log line `rate limiter unavailable` | > 0 in 10 min | Cloud SQL reachability; `/ask` fails closed with 503 until it recovers |
| `router-errors` | log line `router fallback` | > 20 in 10 min | Groq 429/quota; answers still work via the deterministic fallback |

### On-call checklist

1. Acknowledge the alert; open its documentation link.
2. `curl -s "$URL/readyz"`: which dependency is false? Is `llm_enabled` false (kill switch on)?
3. Was there a deploy in the last hour? `gcloud run revisions list --service "$SVC" --region "$REGION" --limit 3`.
   If yes and the smoke summary artifact of that run looks off, roll back (section 5) first, debug second.
4. `gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=$SVC AND severity>=WARNING" --limit 50 --freshness 30m`.
5. Cost or abuse? Flip the kill switch (section 7); it takes effect on the next request.
6. Run `scripts/smoke.py` against the service (section 3) to confirm recovery; resolve the incident.
7. Write down what happened and whether an alert threshold or runbook step should change.

## 9. Common incidents

| Symptom | Likely cause | Action |
|---|---|---|
| New revision never becomes ready; deploy step times out | Startup probe hits `/readyz`: Neo4j or Postgres unreachable, or a secret has no version | `gcloud logging read ... severity>=WARNING`; check AuraDB status (paused instance?), `gcloud sql instances describe kgrag-${ENV}-pg`, `gcloud secrets versions list kgrag-${ENV}-<name>`. CD rolls back automatically. |
| `/readyz` 503 on serving instances, `/healthz` 200 | Dependency outage after start (liveness stays green by design, so instances are not restart-looped) | Fix the dependency; the Postgres pool and Neo4j driver reconnect on their own (verified locally by stopping and restarting Postgres). |
| Many `refused: true` answers, `fallback_reason` = `router_error:...` in `routing_log` | Groq 429s / daily quota, or spend cap reached (`BudgetExceeded`) | `SELECT fallback_reason, count(*) FROM routing_log WHERE ts > now() - interval '1 hour' GROUP BY 1;` and `SELECT status, count(*), sum(usd) FROM llm_spend_ledger GROUP BY 1;` Raise the Groq tier, lower traffic, or flip the kill switch. |
| Clients get 429 | Per-key token bucket (`KGRAG_RATE_LIMIT_PER_MIN` / `_BURST`), shared by all instances in `rate_limit_buckets` | Expected under bursts; raise `rate_limit_per_min` / `rate_limit_burst` in tfvars if legitimate. To unblock one client now: `DELETE FROM rate_limit_buckets WHERE key = 'key:<sha256 prefix>';` (as `kgrag_owner`). |
| `/ask` returns 503 `rate limiter unavailable` | The shared limiter cannot reach Cloud SQL (fails closed by design) | Same as a Cloud SQL outage: `/readyz` shows `postgres: false`; fix the database, nothing to do in the app. |
| Spend cap reached although Groq shows less | Reservations at worst case for calls that timed out (kept on purpose: a timeout may still be billed) | `SELECT id, ts, estimate_usd FROM llm_spend_ledger WHERE status = 'reserved' AND ts < now() - interval '10 minutes';` After checking Groq usage, release them as `kgrag_owner`: `UPDATE llm_spend_ledger SET status = 'released', usd = 0, settled_at = now() WHERE id IN (...);` |
| Clients get 401 | Missing/rotated API key | Check the client key against the current secret version (comma list during rotation). |
| Migration job fails with `MigrationDrift` | An applied migration file was edited | Restore the original file from git; put the change in a new `NNNN_*.sql`. |
| Migration job fails on `CREATE EXTENSION vector` | Migrating user lacks `cloudsqlsuperuser` | Run the job as `kgrag_owner` (Terraform default); never as `kgrag_app`. |
| p95 latency regression | Groq latency, cold starts (min 0 in staging), or graph fallbacks | Compare `latency_ms` by `final_route` in `routing_log`; raise `min_instances`. |
| Cost spike | Traffic or LLM tokens | Cloud Billing reports per SKU; LLM spend is not visible to GCP billing: check the Groq console. |

## 10. Local equivalents

```bash
docker compose -p p1-kgrag up -d neo4j postgres
uv run python -m kgrag.migrate up        # or: docker compose -p p1-kgrag --profile api run --rm migrate
uv run kgrag-api                          # 127.0.0.1:8101, no key needed on loopback
python scripts/smoke.py --base-url http://127.0.0.1:8101 --expect-router-fallback --skip-rate-limit
docker compose -p p1-kgrag stop

# Cloud-like: API container as kgrag_app, shared limits in Postgres, plus a kill-switch instance
docker compose -p p1-kgrag --profile api --profile smoke up -d --build
SMOKE_API_KEY=$(grep '^KGRAG_API_KEY=' .env | cut -d= -f2-) python scripts/smoke.py \
  --base-url http://127.0.0.1:8101 --kill-switch-base-url http://127.0.0.1:8111 \
  --require-least-privilege --json-out results/smoke_local.json
# On an empty stack add -f docker-compose.yml -f smoke/compose.smoke.yml to load the seed fixture.
```
