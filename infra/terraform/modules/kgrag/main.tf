locals {
  name = "kgrag-${var.env}"
  labels = merge(
    {
      app        = "kg-rag-enterprise"
      env        = var.env
      managed-by = "terraform"
    },
    var.labels,
  )
  registry_url = "${var.region}-docker.pkg.dev/${var.project_id}/${var.registry_repository_id}"

  services = toset([
    "artifactregistry.googleapis.com",
    "cloudtrace.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "sqladmin.googleapis.com",
  ])
}

resource "google_project_service" "apis" {
  for_each = local.services

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false # shared by both environments in one project; never switch APIs off on destroy
}
