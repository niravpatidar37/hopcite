terraform {
  required_version = ">= 1.11.0"

  # Remote state in GCS, configured at init time (bucket created during bootstrap, see README):
  #   terraform init -backend-config="bucket=<PROJECT_ID>-tfstate" -backend-config="prefix=kgrag/staging"
  backend "gcs" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.7"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }
}
