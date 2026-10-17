# Shared Docker repository. Created by the staging environment; prod deploys the same digest from it.

resource "google_artifact_registry_repository" "images" {
  count = var.create_registry ? 1 : 0

  # checkov:skip=CKV_GCP_84:Google-managed encryption; CMEK is a documented hardening step.
  project       = var.project_id
  location      = var.region
  repository_id = var.registry_repository_id
  description   = "kg-rag-enterprise container images (deployed by digest)"
  format        = "DOCKER"
  labels        = local.labels

  docker_config {
    immutable_tags = true
  }

  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "keep-recent-releases"
    action = "KEEP"

    most_recent_versions {
      keep_count = 30
    }
  }

  cleanup_policies {
    id     = "delete-old-untagged"
    action = "DELETE"

    condition {
      tag_state  = "UNTAGGED"
      older_than = "2592000s" # 30 days
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_artifact_registry_repository_iam_member" "deployer_push" {
  count = var.create_registry && local.deployer != "" ? 1 : 0

  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.images[0].name
  role       = "roles/artifactregistry.writer"
  member     = local.deployer
}
