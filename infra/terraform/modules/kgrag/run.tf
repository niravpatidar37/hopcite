# Cloud Run: the API service plus two jobs (schema migrations, ingestion), all from the same image.
# Created only when var.deploy_services is true (second apply, after secrets have versions and an
# image exists). After creation, the CD workflow owns the image and traffic split; Terraform
# ignores those fields so a plan never rolls a deploy back.

locals {
  vpc_egress = {
    subnetwork = google_compute_subnetwork.run.id
  }

  common_env = {
    KGRAG_LOAD_DOTENV = "0"
    KGRAG_LOG_FORMAT  = "json"
    KGRAG_KILL_SWITCH = var.kill_switch ? "1" : "0"
    LLM_SPEND_CAP_USD = tostring(var.llm_spend_cap_usd)
    LLM_SPEND_LEDGER  = "/tmp/kgrag-spend-ledger.jsonl"
    PG_HOST           = google_sql_database_instance.pg.private_ip_address
    PG_PORT           = "5432"
    PG_DB             = google_sql_database.kgrag.name
    PG_SSLMODE        = "require"
  }

  api_env = merge(local.common_env, {
    KGRAG_HOST               = "0.0.0.0"
    KGRAG_ROUTING_LOG        = "postgres"
    KGRAG_LLM_CACHE          = "0"
    KGRAG_ANSWER_PROVIDER    = "groq"
    KGRAG_ANSWER_MODEL       = var.answer_model
    KGRAG_RATE_LIMIT_PER_MIN = tostring(var.rate_limit_per_min)
    PG_USER                  = google_sql_user.app.name
  })

  job_env = merge(local.common_env, {
    PG_USER        = google_sql_user.owner.name
    SEC_USER_AGENT = var.sec_user_agent
  })

  api_secret_env = {
    KGRAG_API_KEY     = "api-key"
    GROQ_API_KEY      = "groq-api-key"
    NEO4J_URI         = "neo4j-uri"
    NEO4J_USER        = "neo4j-user"
    NEO4J_PASSWORD    = "neo4j-password"  # gitleaks:allow (secret *name*, not a value)
    POSTGRES_PASSWORD = "pg-app-password" # gitleaks:allow (secret *name*, not a value)
  }

  job_secret_env = {
    GROQ_API_KEY      = "groq-api-key"
    NEO4J_URI         = "neo4j-uri"
    NEO4J_USER        = "neo4j-user"
    NEO4J_PASSWORD    = "neo4j-password"    # gitleaks:allow (secret *name*, not a value)
    POSTGRES_PASSWORD = "pg-owner-password" # gitleaks:allow (secret *name*, not a value)
  }

  api_audience = "${local.name}-api"
}

resource "google_cloud_run_v2_service" "api" {
  count = var.deploy_services ? 1 : 0

  project             = var.project_id
  name                = "${local.name}-api"
  location            = var.region
  ingress             = var.ingress
  deletion_protection = var.deletion_protection
  custom_audiences    = [local.api_audience] # lets CI mint one ID token valid for the service and its tag URLs
  labels              = local.labels

  template {
    service_account                  = google_service_account.api.email
    timeout                          = var.request_timeout
    max_instance_request_concurrency = var.concurrency
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    labels                           = local.labels

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY" # Cloud SQL via the VPC; AuraDB and Groq over the internet

      network_interfaces {
        subnetwork = local.vpc_egress.subnetwork
      }
    }

    containers {
      name  = "api"
      image = var.image

      ports {
        container_port = 8101
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      dynamic "env" {
        for_each = local.api_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.api_secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.s[env.value].secret_id
              version = "latest" # resolved at instance start; rotate = add version + new revision
            }
          }
        }
      }

      # Startup: the instance gets traffic only once Neo4j and Postgres answer (/readyz).
      startup_probe {
        initial_delay_seconds = 0
        period_seconds        = 5
        timeout_seconds       = 3
        failure_threshold     = 24

        http_get {
          path = "/readyz"
          port = 8101
        }
      }

      # Liveness: process health only, so a dependency outage does not restart-loop instances.
      liveness_probe {
        period_seconds    = 15
        timeout_seconds   = 3
        failure_threshold = 3

        http_get {
          path = "/healthz"
          port = 8101
        }
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      template[0].revision,
      traffic,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_secret_manager_secret_iam_member.readers,
    google_secret_manager_secret_version.pg_app,
    google_project_iam_member.telemetry,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  count = var.deploy_services && var.public_invoker ? 1 : 0

  # checkov:skip=CKV_GCP_102:Opt-in (var.public_invoker). The app enforces API keys on /ask and refuses to start without one.
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api[0].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "deployer_invoke" {
  count = var.deploy_services && local.deployer != "" ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api[0].name
  role     = "roles/run.invoker"
  member   = local.deployer
}

# --------------------------------------------------------------------------- jobs
resource "google_cloud_run_v2_job" "migrate" {
  count = var.deploy_services ? 1 : 0

  project             = var.project_id
  name                = "${local.name}-migrate"
  location            = var.region
  deletion_protection = var.deletion_protection
  labels              = local.labels

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account       = google_service_account.jobs.email
      timeout               = "600s"
      max_retries           = 0 # a failed migration must stop the deploy, not retry half-applied work
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"

        network_interfaces {
          subnetwork = local.vpc_egress.subnetwork
        }
      }

      containers {
        name    = "migrate"
        image   = var.image
        command = ["python"]
        args    = ["-m", "kgrag.migrate", "up"]

        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi"
          }
        }

        dynamic "env" {
          for_each = local.job_env
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = local.job_secret_env
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.s[env.value].secret_id
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_secret_manager_secret_iam_member.readers,
    google_secret_manager_secret_version.pg_owner,
    google_project_iam_member.telemetry,
  ]
}

resource "google_cloud_run_v2_job" "ingest" {
  count = var.deploy_services ? 1 : 0

  project             = var.project_id
  name                = "${local.name}-ingest"
  location            = var.region
  deletion_protection = var.deletion_protection
  labels              = local.labels

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account       = google_service_account.jobs.email
      timeout               = "3600s"
      max_retries           = 1 # MERGE/upsert are idempotent, so one retry is safe
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"

        network_interfaces {
          subnetwork = local.vpc_egress.subnetwork
        }
      }

      containers {
        name    = "ingest"
        image   = var.image
        command = ["python"]
        # Default: migrate -> fetch EDGAR -> build from the committed extraction cache ($0 LLM).
        # Paid extraction for new filings: execute with --args=-m,kgrag.jobs,ingest,--steps,migrate,fetch,extract,build
        args = ["-m", "kgrag.jobs", "ingest", "--steps", "migrate,fetch,build"]

        resources {
          limits = {
            cpu    = "2"
            memory = "4Gi"
          }
        }

        dynamic "env" {
          for_each = local.job_env
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = local.job_secret_env
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.s[env.value].secret_id
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_secret_manager_secret_iam_member.readers,
    google_secret_manager_secret_version.pg_owner,
    google_project_iam_member.telemetry,
  ]
}
