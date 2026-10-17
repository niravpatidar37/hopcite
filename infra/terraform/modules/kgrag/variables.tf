variable "project_id" {
  description = "GCP project that hosts this environment."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Cloud SQL, Artifact Registry and the VPC subnet."
  type        = string
  default     = "us-central1"
}

variable "env" {
  description = "Environment name (staging | prod). Used in every resource name."
  type        = string

  validation {
    condition     = contains(["staging", "prod"], var.env)
    error_message = "env must be staging or prod."
  }
}

variable "deploy_services" {
  description = "Create the Cloud Run service and jobs. Leave false for the first apply (network, SQL, secrets, registry), add secret versions and push an image, then set true."
  type        = bool
  default     = false
}

variable "image" {
  description = "Container image by digest (REGION-docker.pkg.dev/PROJECT/kgrag/kg-rag-enterprise@sha256:...). CD updates revisions after the first apply; Terraform ignores later image changes."
  type        = string
  default     = ""

  validation {
    condition     = var.image == "" || can(regex("@sha256:[0-9a-f]{64}$", var.image))
    error_message = "image must be pinned by digest (@sha256:...), not a tag."
  }
}

# ----------------------------------------------------------------------------- registry
variable "create_registry" {
  description = "Create the shared Artifact Registry repository in this environment (staging does; prod reuses it so the same digest is promoted)."
  type        = bool
  default     = false
}

variable "registry_repository_id" {
  description = "Artifact Registry repository id holding the image."
  type        = string
  default     = "kgrag"
}

# ----------------------------------------------------------------------------- Cloud Run sizing
variable "min_instances" {
  description = "Minimum warm instances (0 = scale to zero; cold start loads the embedding model and entity catalog)."
  type        = number
  default     = 0
}

variable "max_instances" {
  description = "Maximum instances. Also bounds total rate-limit capacity and per-instance spend ledgers."
  type        = number
  default     = 3
}

variable "concurrency" {
  description = "Max concurrent requests per instance. /ask is LLM-bound and runs in a thread pool."
  type        = number
  default     = 8
}

variable "cpu" {
  description = "vCPU limit per API instance."
  type        = string
  default     = "1"
}

variable "memory" {
  description = "Memory limit per API instance (embedding model ~70 MB + ONNX runtime + entity catalog)."
  type        = string
  default     = "2Gi"
}

variable "request_timeout" {
  description = "Cloud Run request timeout."
  type        = string
  default     = "120s"
}

variable "ingress" {
  description = "Cloud Run ingress: INGRESS_TRAFFIC_ALL (API-key protected) or INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER (behind an external LB / Cloud Armor)."
  type        = string
  default     = "INGRESS_TRAFFIC_ALL"
}

variable "public_invoker" {
  description = "Grant roles/run.invoker to allUsers so API-key clients without Google identity can call /ask. When false, callers also need a Google ID token."
  type        = bool
  default     = false
}

# ----------------------------------------------------------------------------- app config
variable "answer_model" {
  description = "Groq answer model on Cloud Run (no local Ollama there). The published benchmark used local llama3.1:8b."
  type        = string
  default     = "openai/gpt-oss-20b"
}

variable "rate_limit_per_min" {
  description = "Per-client /ask rate limit per instance (KGRAG_RATE_LIMIT_PER_MIN)."
  type        = number
  default     = 30
}

variable "kill_switch" {
  description = "KGRAG_KILL_SWITCH: true disables every LLM call (/ask answers 503). Emergency use: prefer `gcloud run services update --update-env-vars`, then reconcile here."
  type        = bool
  default     = false
}

variable "llm_spend_cap_usd" {
  description = "LLM_SPEND_CAP_USD for each instance's local ledger."
  type        = number
  default     = 5
}

variable "sec_user_agent" {
  description = "SEC_USER_AGENT for the ingestion job (EDGAR requires a real contact email)."
  type        = string
  default     = ""
}

# ----------------------------------------------------------------------------- Cloud SQL
variable "sql_tier" {
  description = "Cloud SQL machine tier (Enterprise edition)."
  type        = string
  default     = "db-custom-1-3840"
}

variable "sql_high_availability" {
  description = "REGIONAL (HA, synchronous standby) instead of ZONAL."
  type        = bool
  default     = false
}

variable "sql_disk_size_gb" {
  description = "Initial SSD size; autoresize is on."
  type        = number
  default     = 10
}

variable "deletion_protection" {
  description = "Protect Cloud SQL and Cloud Run resources from terraform destroy."
  type        = bool
  default     = true
}

variable "db_password_version" {
  description = "Bump to rotate both database passwords (new random values are written to Cloud SQL and Secret Manager; never stored in state)."
  type        = number
  default     = 1
}

# ----------------------------------------------------------------------------- network
variable "subnet_cidr" {
  description = "Subnet used by Cloud Run Direct VPC egress."
  type        = string
  default     = "10.10.0.0/24"
}

variable "psa_prefix_length" {
  description = "Prefix length of the Private Service Access range reserved for Cloud SQL."
  type        = number
  default     = 20
}

# ----------------------------------------------------------------------------- CI/CD identity
variable "deployer_service_account" {
  description = "Email of the GitHub Actions deploy service account (Workload Identity Federation). Empty = no deploy IAM bindings."
  type        = string
  default     = ""
}

variable "labels" {
  description = "Extra labels on every labelled resource."
  type        = map(string)
  default     = {}
}
