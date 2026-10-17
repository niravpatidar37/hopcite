# Cloud SQL for PostgreSQL 17 (pgvector is a supported extension; `CREATE EXTENSION vector` runs in
# migration 0001 as kgrag_owner). Private IP only, TLS required, PITR backups.
#
# Two built-in users with passwords generated here as *ephemeral* values and passed through
# write-only arguments, so they are written to Cloud SQL and Secret Manager but never to Terraform
# state or plan files. Bump var.db_password_version to rotate both.
#   kgrag_owner: runs migrations and the ingestion job (owns the schema).
#   kgrag_app:   the API's runtime user; R__grants.sql limits it to SELECT chunks / INSERT routing_log.

resource "google_sql_database_instance" "pg" {
  # checkov:skip=CKV_GCP_79:Pinned to PostgreSQL 17 to match the local pgvector/pgvector:0.8.1-pg17 stack the benchmark ran on; upgrade both together.
  # checkov:skip=CKV_GCP_6:ssl_mode = ENCRYPTED_ONLY rejects non-TLS connections; client certificates are not used (password auth over private IP).
  project             = var.project_id
  name                = "${local.name}-pg"
  region              = var.region
  database_version    = "POSTGRES_17"
  deletion_protection = var.deletion_protection

  settings {
    edition                     = "ENTERPRISE"
    tier                        = var.sql_tier
    availability_type           = var.sql_high_availability ? "REGIONAL" : "ZONAL"
    disk_type                   = "PD_SSD"
    disk_size                   = var.sql_disk_size_gb
    disk_autoresize             = true
    deletion_protection_enabled = var.deletion_protection
    user_labels                 = local.labels

    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.vpc.id
      ssl_mode        = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "03:00"
      transaction_log_retention_days = 7

      backup_retention_settings {
        retained_backups = 14
        retention_unit   = "COUNT"
      }
    }

    maintenance_window {
      day          = 7
      hour         = 4
      update_track = "stable"
    }

    insights_config {
      query_insights_enabled  = true
      record_application_tags = false
      record_client_address   = false
    }

    # Logging flags (CIS GCP 6.2.x). Statement text is not logged: queries carry user questions.
    database_flags {
      name  = "log_checkpoints"
      value = "on"
    }
    database_flags {
      name  = "log_connections"
      value = "on"
    }
    database_flags {
      name  = "log_disconnections"
      value = "on"
    }
    database_flags {
      name  = "log_lock_waits"
      value = "on"
    }
    database_flags {
      name  = "log_min_messages"
      value = "error"
    }
    database_flags {
      name  = "log_min_error_statement"
      value = "error"
    }
    database_flags {
      name  = "log_hostname"
      value = "on"
    }
    database_flags {
      name  = "log_duration"
      value = "on"
    }
    database_flags {
      name  = "log_statement"
      value = "ddl" # schema changes only; DML text (user questions as parameters) is not logged
    }
    database_flags {
      name  = "log_temp_files"
      value = "0"
    }
    database_flags {
      name  = "log_min_duration_statement"
      value = "-1"
    }
    database_flags {
      name  = "cloudsql.enable_pgaudit"
      value = "on"
    }
  }

  depends_on = [google_service_networking_connection.psa]
}

resource "google_sql_database" "kgrag" {
  project  = var.project_id
  name     = "kgrag"
  instance = google_sql_database_instance.pg.name
}

ephemeral "random_password" "pg_owner" {
  length  = 40
  special = false
}

ephemeral "random_password" "pg_app" {
  length  = 40
  special = false
}

resource "google_sql_user" "owner" {
  project             = var.project_id
  name                = "kgrag_owner"
  instance            = google_sql_database_instance.pg.name
  type                = "BUILT_IN"
  password_wo         = ephemeral.random_password.pg_owner.result
  password_wo_version = var.db_password_version
}

resource "google_sql_user" "app" {
  project             = var.project_id
  name                = "kgrag_app"
  instance            = google_sql_database_instance.pg.name
  type                = "BUILT_IN"
  password_wo         = ephemeral.random_password.pg_app.result
  password_wo_version = var.db_password_version
}

resource "google_secret_manager_secret_version" "pg_owner" {
  secret                 = google_secret_manager_secret.s["pg-owner-password"].id
  secret_data_wo         = ephemeral.random_password.pg_owner.result
  secret_data_wo_version = var.db_password_version
}

resource "google_secret_manager_secret_version" "pg_app" {
  secret                 = google_secret_manager_secret.s["pg-app-password"].id
  secret_data_wo         = ephemeral.random_password.pg_app.result
  secret_data_wo_version = var.db_password_version
}
