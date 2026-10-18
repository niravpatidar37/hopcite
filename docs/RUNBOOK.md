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

**2e. Least-privilege check for the runtime DB user.** Cloud SQL adds every API-created user to
`cloudsqlsuperuser`; `R__grants.sql` revokes that from `kgrag_app` when the migrating user may do so.
Verify once (Cloud SQL Studio in the console, or any psql session on the VPC):

```sql
SELECT pg_has_role('kgrag_app', 'cloudsqlsuperuser', 'MEMBER');        -- must be false
SELECT has_table_privilege('kgrag_app', 'chunks', 'INSERT');           -- must be false
-- if the first is true, as the postgres user: REVOKE cloudsqlsuperuser FROM kgrag_app;
```

## 3. Routine deploys (CD)

Merging to `main` runs `.github/workflows/deploy.yml`:

1. Build the image once, push `…/hopcite:<sha>-<attempt>`, resolve the **digest**.
2. Staging: point both jobs at the digest, execute `migrate`, deploy the service, smoke test
   (`scripts/smoke_test.sh`: `/healthz` 200, `/readyz` 200 with both stores, `POST /ask` without a key is 401).
   Failure rolls staging back to its previous revision.
3. Prod waits for approval on the `production` environment, then: migrate, deploy a `candidate`
   revision with **0% traffic**, smoke its tag URL, shift `CANARY_PERCENT` (default 10%), smoke for
   `CANARY_SOAK_SECONDS` (default 300 s), promote to 100%, smoke again. Any failure moves 100% back to
   the revision that was serving before the run.

Manual run: Actions -> deploy -> Run workflow (main only; WIF rejects other refs).

Infrastructure changes are **not** applied by CD. Run `terraform plan` / `apply` by hand per
environment; CD owns only the image and traffic (Terraform ignores both).

## 4. Verify

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
| `api-key` | Zero downtime: add a version holding `NEW,OLD` (comma-separated, both accepted), roll a revision, move clients to NEW, add a version holding `NEW`, roll again, `gcloud secrets versions disable <old> --secret=kgrag-${ENV}-api-key`. |
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

## 8. Common incidents

| Symptom | Likely cause | Action |
|---|---|---|
| New revision never becomes ready; deploy step times out | Startup probe hits `/readyz`: Neo4j or Postgres unreachable, or a secret has no version | `gcloud logging read ... severity>=WARNING`; check AuraDB status (paused instance?), `gcloud sql instances describe kgrag-${ENV}-pg`, `gcloud secrets versions list kgrag-${ENV}-<name>`. CD rolls back automatically. |
| `/readyz` 503 on serving instances, `/healthz` 200 | Dependency outage after start (liveness stays green by design, so instances are not restart-looped) | Fix the dependency; the Postgres pool and Neo4j driver reconnect on their own (verified locally by stopping and restarting Postgres). |
| Many `refused: true` answers, `fallback_reason` = `router_error:...` in `routing_log` | Groq 429s / daily quota, or spend cap reached (`BudgetExceeded`) | `SELECT fallback_reason, count(*) FROM routing_log WHERE ts > now() - interval '1 hour' GROUP BY 1;` Raise the Groq tier, lower traffic, or flip the kill switch. |
| Clients get 429 | Per-key rate limit (`KGRAG_RATE_LIMIT_PER_MIN`, per instance) | Expected under bursts; raise the limit in tfvars if legitimate. |
| Clients get 401 | Missing/rotated API key | Check the client key against the current secret version (comma list during rotation). |
| Migration job fails with `MigrationDrift` | An applied migration file was edited | Restore the original file from git; put the change in a new `NNNN_*.sql`. |
| Migration job fails on `CREATE EXTENSION vector` | Migrating user lacks `cloudsqlsuperuser` | Run the job as `kgrag_owner` (Terraform default); never as `kgrag_app`. |
| p95 latency regression | Groq latency, cold starts (min 0 in staging), or graph fallbacks | Compare `latency_ms` by `final_route` in `routing_log`; raise `min_instances`. |
| Cost spike | Traffic or LLM tokens | Cloud Billing reports per SKU; LLM spend is not visible to GCP billing: check the Groq console. |

## 9. Local equivalents

```bash
docker compose -p p1-kgrag up -d neo4j postgres
uv run python -m kgrag.migrate up        # or: docker compose -p p1-kgrag --profile api run --rm migrate
uv run kgrag-api                          # 127.0.0.1:8101, no key needed on loopback
bash scripts/smoke_test.sh http://127.0.0.1:8101
docker compose -p p1-kgrag stop
```
