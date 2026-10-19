variable "project_id" {
  description = "GCP project id (pass with -var or TF_VAR_project_id; not committed)."
  type        = string
}

variable "region" {
  description = "GCP region."
  type        = string
  default     = "us-central1"
}

variable "image" {
  description = "Image by digest for the first deploy_services apply; CD owns it afterwards."
  type        = string
  default     = ""
}

variable "deploy_services" {
  description = "Second-phase switch: create the Cloud Run service and jobs."
  type        = bool
  default     = false
}

variable "deployer_service_account" {
  description = "GitHub Actions deploy service account email (Workload Identity Federation)."
  type        = string
  default     = ""
}

variable "sec_user_agent" {
  description = "SEC_USER_AGENT for the ingestion job (descriptive app name + real contact email)."
  type        = string
  default     = ""
}

variable "min_instances" {
  description = "Minimum Cloud Run instances."
  type        = number
}

variable "max_instances" {
  description = "Maximum Cloud Run instances."
  type        = number
}

variable "concurrency" {
  description = "Concurrent requests per instance."
  type        = number
}

variable "cpu" {
  description = "vCPU per instance."
  type        = string
}

variable "memory" {
  description = "Memory per instance."
  type        = string
}

variable "sql_tier" {
  description = "Cloud SQL tier."
  type        = string
}

variable "sql_high_availability" {
  description = "Regional (HA) Cloud SQL."
  type        = bool
}

variable "deletion_protection" {
  description = "Protect stateful resources from destroy."
  type        = bool
}

variable "rate_limit_per_min" {
  description = "Per-client /ask limit across all instances (shared in Cloud SQL)."
  type        = number
}

variable "llm_spend_cap_usd" {
  description = "Global LLM spend cap (shared llm_spend_ledger table)."
  type        = number
}

variable "public_invoker" {
  description = "Allow unauthenticated (API-key only) invocation."
  type        = bool
}

variable "kill_switch" {
  description = "Disable all LLM calls."
  type        = bool
  default     = false
}

variable "rate_limit_burst" {
  description = "Per-client token-bucket burst."
  type        = number
  default     = 10
}

variable "alert_email" {
  description = "On-call email for alert notifications (pass with TF_VAR_alert_email; not committed). Empty = no channel."
  type        = string
  default     = ""
}
