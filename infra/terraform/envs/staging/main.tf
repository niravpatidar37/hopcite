provider "google" {
  project = var.project_id
  region  = var.region

  default_labels = {
    app = "hopcite"
    env = "staging"
  }
}

module "kgrag" {
  source = "../../modules/kgrag"

  project_id               = var.project_id
  region                   = var.region
  env                      = "staging"
  deploy_services          = var.deploy_services
  image                    = var.image
  create_registry          = true
  deployer_service_account = var.deployer_service_account
  sec_user_agent           = var.sec_user_agent

  min_instances         = var.min_instances
  max_instances         = var.max_instances
  concurrency           = var.concurrency
  cpu                   = var.cpu
  memory                = var.memory
  sql_tier              = var.sql_tier
  sql_high_availability = var.sql_high_availability
  deletion_protection   = var.deletion_protection
  rate_limit_per_min    = var.rate_limit_per_min
  rate_limit_burst      = var.rate_limit_burst
  alert_email           = var.alert_email
  llm_spend_cap_usd     = var.llm_spend_cap_usd
  public_invoker        = var.public_invoker
  kill_switch           = var.kill_switch
  subnet_cidr           = "10.10.0.0/24"
}
