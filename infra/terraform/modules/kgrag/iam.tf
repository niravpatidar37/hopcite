# One dedicated service account per workload, each with only what it needs:
#   api:  read its secrets, write logs/metrics/traces. No project-wide data roles.
#   jobs: same for the migration and ingestion jobs (owner DB password instead of the app one).
# Cloud SQL is reached over private IP with a database password, so no roles/cloudsql.client.

resource "google_service_account" "api" {
  project      = var.project_id
  account_id   = "${local.name}-api"
  display_name = "kg-rag-enterprise API (${var.env})"

  depends_on = [google_project_service.apis]
}

resource "google_service_account" "jobs" {
  project      = var.project_id
  account_id   = "${local.name}-jobs"
  display_name = "kg-rag-enterprise migration + ingestion jobs (${var.env})"

  depends_on = [google_project_service.apis]
}

locals {
  telemetry_roles = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/cloudtrace.agent",
  ])
  runtime_members = {
    api  = google_service_account.api.member
    jobs = google_service_account.jobs.member
  }
  telemetry_bindings = merge([
    for sa, member in local.runtime_members : {
      for role in local.telemetry_roles : "${sa}:${role}" => { member = member, role = role }
    }
  ]...)
  deployer = var.deployer_service_account == "" ? "" : "serviceAccount:${var.deployer_service_account}"
}

resource "google_project_iam_member" "telemetry" {
  for_each = local.telemetry_bindings

  project = var.project_id
  role    = each.value.role
  member  = each.value.member
}

# --------------------------------------------------------------------------- GitHub Actions deployer (WIF)
# The deploy SA itself is created once during bootstrap (README "Deployment"); here it gets:
# deploy Cloud Run revisions/jobs, act as the two runtime SAs, push images, and invoke the
# service for smoke tests.
resource "google_project_iam_member" "deployer_run" {
  count = local.deployer == "" ? 0 : 1

  project = var.project_id
  role    = "roles/run.developer"
  member  = local.deployer
}

resource "google_service_account_iam_member" "deployer_act_as" {
  for_each = local.deployer == "" ? {} : {
    api  = google_service_account.api.name
    jobs = google_service_account.jobs.name
  }

  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = local.deployer
}
