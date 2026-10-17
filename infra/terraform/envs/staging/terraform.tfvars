# Staging: scale to zero, small zonal Cloud SQL, cheap to leave running.
# project_id, image, deployer_service_account and sec_user_agent are passed at plan time
# (-var / TF_VAR_*), so nothing environment-identifying is committed.
min_instances         = 0
max_instances         = 2
concurrency           = 8
cpu                   = "1"
memory                = "2Gi"
sql_tier              = "db-custom-1-3840"
sql_high_availability = false
deletion_protection   = false
rate_limit_per_min    = 30
llm_spend_cap_usd     = 1
public_invoker        = false
