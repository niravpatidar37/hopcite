output "service_name" {
  description = "Cloud Run service name (empty until deploy_services = true)."
  value       = var.deploy_services ? google_cloud_run_v2_service.api[0].name : ""
}

output "service_url" {
  description = "Cloud Run service URL."
  value       = var.deploy_services ? google_cloud_run_v2_service.api[0].uri : ""
}

output "service_audience" {
  description = "Custom audience for ID tokens (CI smoke tests)."
  value       = local.api_audience
}

output "migrate_job" {
  description = "Cloud Run Job that applies schema migrations."
  value       = var.deploy_services ? google_cloud_run_v2_job.migrate[0].name : ""
}

output "ingest_job" {
  description = "Cloud Run Job that (re)builds the graph and vector index."
  value       = var.deploy_services ? google_cloud_run_v2_job.ingest[0].name : ""
}

output "registry_url" {
  description = "Docker registry path for images."
  value       = local.registry_url
}

output "sql_instance" {
  description = "Cloud SQL instance name."
  value       = google_sql_database_instance.pg.name
}

output "sql_private_ip" {
  description = "Cloud SQL private IP (PG_HOST)."
  value       = google_sql_database_instance.pg.private_ip_address
}

output "api_service_account" {
  description = "Runtime service account of the API."
  value       = google_service_account.api.email
}

output "jobs_service_account" {
  description = "Runtime service account of the jobs."
  value       = google_service_account.jobs.email
}

output "secret_ids" {
  description = "Secret Manager secret ids (values are added out of band)."
  value       = { for k, s in google_secret_manager_secret.s : k => s.secret_id }
}
