# Prod: one warm instance (no cold start for the first user), regional HA Cloud SQL, deletion
# protection on. project_id, image, deployer_service_account and sec_user_agent are passed at plan
# time (-var / TF_VAR_*).
min_instances         = 1
max_instances         = 5
concurrency           = 8
cpu                   = "1"
memory                = "2Gi"
sql_tier              = "db-custom-2-7680"
sql_high_availability = true
deletion_protection   = true
rate_limit_per_min    = 30
llm_spend_cap_usd     = 5
public_invoker        = true
