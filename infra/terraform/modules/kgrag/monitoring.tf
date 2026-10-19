# Monitoring as code: uptime check, SLO + multi-window burn-rate alerts, golden-signal alerts, Cloud SQL
# alerts and app-specific log-based alerts. Every policy links to docs/RUNBOOK.md. Notifications go to
# var.alert_email when set (policies are created either way and show in the console).
#
# Status: validated with `terraform validate` against hashicorp/google 8.7.0 (lock file); NOT applied
# (no cloud account connected), so thresholds are untested starting points from docs/SLO.md.

locals {
  service_name     = "${local.name}-api"
  run_filter       = "resource.type=\"cloud_run_revision\" AND resource.label.\"service_name\"=\"${local.service_name}\""
  sql_filter       = "resource.type=\"cloudsql_database\" AND resource.label.\"database_id\"=\"${var.project_id}:${google_sql_database_instance.pg.name}\""
  channels         = google_monitoring_notification_channel.email[*].id
  runbook          = "https://github.com/niravpatidar37/hopcite/blob/main/docs/RUNBOOK.md"
  monitoring_count = var.deploy_services ? 1 : 0

  # App log lines (JSON on stdout -> jsonPayload) that should page. Message strings are pinned by
  # tests/test_monitoring_contract.py so a code change cannot silently break an alert.
  log_alerts = {
    db-role-privileged = {
      message   = "db role not least-privilege"
      threshold = 0
      doc       = "The runtime DB user kept superuser-like rights (e.g. cloudsqlsuperuser). Least-privilege grants are void until fixed. RUNBOOK section 2e."
    }
    spend-cap-refusals = {
      message   = "llm spend cap reached"
      threshold = 0
      doc       = "LLM calls are being refused by the shared spend cap (llm_spend_ledger). Answers degrade to refusals. RUNBOOK section 9."
    }
    rate-limiter-unavailable = {
      message   = "rate limiter unavailable"
      threshold = 0
      doc       = "The shared Postgres rate limiter is unreachable; /ask fails closed with 503. Check Cloud SQL. RUNBOOK section 9."
    }
    router-errors = {
      message   = "router fallback"
      threshold = 20
      doc       = "The router LLM keeps failing (Groq 429/quota or spend cap); queries fall back to the deterministic both-paths route. RUNBOOK section 9."
    }
  }
}

data "google_project" "this" {
  project_id = var.project_id
}

resource "google_monitoring_notification_channel" "email" {
  count = var.alert_email == "" ? 0 : 1

  project      = var.project_id
  display_name = "hopcite ${var.env} on-call"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
  user_labels = local.labels

  depends_on = [google_project_service.apis]
}

# --------------------------------------------------------------------------- uptime check on /healthz
# Liveness only (no dependency calls), so it measures "is the service up", not "are the DBs up".
# Authenticates with an OIDC token from the Monitoring service agent, which needs run.invoker when
# the service is not public.
resource "google_monitoring_uptime_check_config" "healthz" {
  count = local.monitoring_count

  project      = var.project_id
  display_name = "${local.service_name} /healthz"
  timeout      = "10s"
  period       = "60s"
  user_labels  = local.labels

  http_check {
    path           = "/healthz"
    port           = 443
    use_ssl        = true
    validate_ssl   = true
    request_method = "GET"

    accepted_response_status_codes {
      status_value = 200
    }

    service_agent_authentication {
      type = "OIDC_TOKEN"
    }
  }

  content_matchers {
    content = "\"ok\""
    matcher = "CONTAINS_STRING"
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = trimprefix(google_cloud_run_v2_service.api[0].uri, "https://")
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "uptime_invoker" {
  count = local.monitoring_count

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-monitoring-notification.iam.gserviceaccount.com"
}

resource "google_monitoring_alert_policy" "uptime" {
  count = local.monitoring_count

  project               = var.project_id
  display_name          = "${local.service_name}: /healthz uptime check failing"
  combiner              = "OR"
  notification_channels = local.channels
  user_labels           = local.labels

  conditions {
    display_name = "uptime check failed from 2+ regions"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.\"check_id\"=\"${google_monitoring_uptime_check_config.healthz[0].uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "120s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.\"host\""]
      }
    }
  }

  documentation {
    content   = "The /healthz uptime check fails from more than one checker region: the service is down or unreachable. Runbook: ${local.runbook} (sections 4, 5, 9)."
    mime_type = "text/markdown"
  }
}

# --------------------------------------------------------------------------- golden signals
resource "google_monitoring_alert_policy" "error_ratio" {
  count = local.monitoring_count

  project               = var.project_id
  display_name          = "${local.service_name}: 5xx ratio > ${var.alert_5xx_ratio * 100}%"
  combiner              = "OR"
  notification_channels = local.channels
  user_labels           = local.labels

  conditions {
    display_name = "5xx / all requests over 5 min"
    condition_threshold {
      filter             = "metric.type=\"run.googleapis.com/request_count\" AND ${local.run_filter} AND metric.label.\"response_code_class\"=\"5xx\""
      denominator_filter = "metric.type=\"run.googleapis.com/request_count\" AND ${local.run_filter}"
      comparison         = "COMPARISON_GT"
      threshold_value    = var.alert_5xx_ratio
      duration           = "300s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
      denominator_aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    content   = "More than ${var.alert_5xx_ratio * 100}% of requests return 5xx. 503s are expected while the kill switch is on (check /readyz llm_enabled). Runbook: ${local.runbook} (section 9)."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "latency_p95" {
  count = local.monitoring_count

  project               = var.project_id
  display_name          = "${local.service_name}: p95 latency > ${var.slo_latency_p95_ms} ms"
  combiner              = "OR"
  notification_channels = local.channels
  user_labels           = local.labels

  conditions {
    display_name = "p95 request latency over 10 min"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/request_latencies\" AND ${local.run_filter}"
      comparison      = "COMPARISON_GT"
      threshold_value = var.slo_latency_p95_ms
      duration        = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_PERCENTILE_95"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }

  documentation {
    content   = "p95 latency is above the SLO (docs/SLO.md). Compare latency_ms by final_route in routing_log; check Groq latency and cold starts. Runbook: ${local.runbook} (section 9)."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "saturation" {
  count = local.monitoring_count

  project               = var.project_id
  display_name          = "${local.service_name}: instance saturation"
  combiner              = "OR"
  notification_channels = local.channels
  user_labels           = local.labels

  conditions {
    display_name = "active instances at max_instances for 10 min"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/container/instance_count\" AND ${local.run_filter} AND metric.label.\"state\"=\"active\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.max_instances - 0.5
      duration        = "600s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  conditions {
    display_name = "container CPU p99 > 85% for 10 min"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/container/cpu/utilizations\" AND ${local.run_filter}"
      comparison      = "COMPARISON_GT"
      threshold_value = 0.85
      duration        = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_PERCENTILE_99"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }

  conditions {
    display_name = "container memory p99 > 90% for 10 min"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/container/memory/utilizations\" AND ${local.run_filter}"
      comparison      = "COMPARISON_GT"
      threshold_value = 0.9
      duration        = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_PERCENTILE_99"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }

  documentation {
    content   = "The service is at its scaling ceiling or its instances are CPU/memory bound; requests will queue and time out. Raise max_instances/cpu/memory in tfvars or shed load. Runbook: ${local.runbook} (section 9)."
    mime_type = "text/markdown"
  }
}

# --------------------------------------------------------------------------- SLO + error-budget burn
resource "google_monitoring_custom_service" "api" {
  count = local.monitoring_count

  project      = var.project_id
  service_id   = local.service_name
  display_name = "hopcite API (${var.env})"
  user_labels  = local.labels
}

resource "google_monitoring_slo" "availability" {
  count = local.monitoring_count

  project             = var.project_id
  service             = google_monitoring_custom_service.api[0].service_id
  slo_id              = "availability-28d"
  display_name        = "${local.service_name}: ${var.slo_availability_goal * 100}% of requests not 5xx (28 days)"
  goal                = var.slo_availability_goal
  rolling_period_days = 28
  user_labels         = local.labels

  request_based_sli {
    good_total_ratio {
      bad_service_filter   = "metric.type=\"run.googleapis.com/request_count\" AND ${local.run_filter} AND metric.label.\"response_code_class\"=\"5xx\""
      total_service_filter = "metric.type=\"run.googleapis.com/request_count\" AND ${local.run_filter}"
    }
  }
}

# Multi-window, multi-burn-rate alerts (Google SRE workbook, ch. 5): page when the budget burns fast
# (14.4x over 1 h, confirmed over 5 min = 2% of a 30-day budget in an hour) or steadily (6x over 6 h,
# confirmed over 30 min). The short window stops the alert as soon as the burn stops.
resource "google_monitoring_alert_policy" "burn_rate" {
  for_each = var.deploy_services ? {
    fast = { long = "3600s", short = "300s", rate = 14.4 }
    slow = { long = "21600s", short = "1800s", rate = 6 }
  } : {}

  project               = var.project_id
  display_name          = "${local.service_name}: error budget burn (${each.key}, ${each.value.rate}x)"
  combiner              = "AND"
  notification_channels = local.channels
  user_labels           = local.labels

  conditions {
    display_name = "burn rate > ${each.value.rate} over ${each.value.long}"
    condition_threshold {
      filter          = "select_slo_burn_rate(\"${google_monitoring_slo.availability[0].name}\", \"${each.value.long}\")"
      comparison      = "COMPARISON_GT"
      threshold_value = each.value.rate
      duration        = "0s"
    }
  }

  conditions {
    display_name = "burn rate > ${each.value.rate} over ${each.value.short}"
    condition_threshold {
      filter          = "select_slo_burn_rate(\"${google_monitoring_slo.availability[0].name}\", \"${each.value.short}\")"
      comparison      = "COMPARISON_GT"
      threshold_value = each.value.rate
      duration        = "0s"
    }
  }

  documentation {
    content   = "The availability error budget (docs/SLO.md) is burning at ${each.value.rate}x. Error budget policy: freeze feature deploys; look at Groq and AuraDB first. Runbook: ${local.runbook} (sections 5, 7, 9)."
    mime_type = "text/markdown"
  }
}

# --------------------------------------------------------------------------- Cloud SQL
resource "google_monitoring_alert_policy" "cloudsql" {
  project               = var.project_id
  display_name          = "${google_sql_database_instance.pg.name}: Cloud SQL down or saturated"
  combiner              = "OR"
  notification_channels = local.channels
  user_labels           = local.labels

  conditions {
    display_name = "instance down"
    condition_threshold {
      filter          = "metric.type=\"cloudsql.googleapis.com/database/up\" AND ${local.sql_filter}"
      comparison      = "COMPARISON_LT"
      threshold_value = 1
      duration        = "180s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MIN"
      }
    }
  }

  conditions {
    display_name = "CPU > 80% for 10 min"
    condition_threshold {
      filter          = "metric.type=\"cloudsql.googleapis.com/database/cpu/utilization\" AND ${local.sql_filter}"
      comparison      = "COMPARISON_GT"
      threshold_value = 0.8
      duration        = "600s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  conditions {
    display_name = "disk > 85%"
    condition_threshold {
      filter          = "metric.type=\"cloudsql.googleapis.com/database/disk/utilization\" AND ${local.sql_filter}"
      comparison      = "COMPARISON_GT"
      threshold_value = 0.85
      duration        = "300s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }

  conditions {
    display_name = "connections > 80 (pools: 8 per API instance)"
    condition_threshold {
      filter          = "metric.type=\"cloudsql.googleapis.com/database/postgresql/num_backends\" AND ${local.sql_filter}"
      comparison      = "COMPARISON_GT"
      threshold_value = 80
      duration        = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    content   = "Cloud SQL (vectors, routing log, shared rate limits, spend ledger) is down or saturated. /readyz fails and /ask returns 503 while it is. Runbook: ${local.runbook} (sections 5, 9)."
    mime_type = "text/markdown"
  }
}

# --------------------------------------------------------------------------- app-specific (log-based)
resource "google_logging_metric" "app" {
  for_each = local.log_alerts

  project = var.project_id
  name    = "${local.name}-${each.key}"
  # Logging query language: OR binds tighter than AND, so every group is parenthesized.
  filter = "((resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${local.service_name}\") OR (resource.type=\"cloud_run_job\" AND resource.labels.job_name=~\"^${local.name}-\")) AND jsonPayload.message=\"${each.value.message}\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "app" {
  for_each = local.log_alerts

  project               = var.project_id
  display_name          = "${local.name}: ${each.key}"
  combiner              = "OR"
  notification_channels = local.channels
  user_labels           = local.labels

  conditions {
    display_name = "\"${each.value.message}\" logged > ${each.value.threshold} times in 10 min"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.app[each.key].name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = each.value.threshold
      duration        = "0s"
      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  alert_strategy {
    auto_close = "3600s"
  }

  documentation {
    content   = "${each.value.doc} Runbook: ${local.runbook}"
    mime_type = "text/markdown"
  }
}
