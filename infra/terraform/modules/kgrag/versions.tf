terraform {
  required_version = ">= 1.11.0" # write-only arguments (password_wo, secret_data_wo) and ephemeral resources

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
