output "service_url" {
  description = "Cloud Run service URL."
  value       = module.kgrag.service_url
}

output "service_name" {
  description = "Cloud Run service name."
  value       = module.kgrag.service_name
}

output "service_audience" {
  description = "ID-token audience for smoke tests."
  value       = module.kgrag.service_audience
}

output "migrate_job" {
  description = "Migration job name."
  value       = module.kgrag.migrate_job
}

output "ingest_job" {
  description = "Ingestion job name."
  value       = module.kgrag.ingest_job
}

output "registry_url" {
  description = "Artifact Registry path."
  value       = module.kgrag.registry_url
}

output "sql_instance" {
  description = "Cloud SQL instance."
  value       = module.kgrag.sql_instance
}

output "secret_ids" {
  description = "Secret Manager secret ids to populate."
  value       = module.kgrag.secret_ids
}
