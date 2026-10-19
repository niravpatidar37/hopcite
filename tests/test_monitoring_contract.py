"""The log-based alerts in infra/terraform/modules/kgrag/monitoring.tf match exact log messages.
These tests pin both sides so renaming a log line cannot silently disable its alert."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TF = (ROOT / "infra" / "terraform" / "modules" / "kgrag" / "monitoring.tf").read_text(encoding="utf-8")
SRC = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src" / "kgrag").glob("*.py"))


def test_every_log_alert_message_is_emitted_by_the_code() -> None:
    messages = re.findall(r'message\s*=\s*"([^"]+)"', TF)
    assert len(messages) == 4
    for m in messages:
        assert re.search(r'log\.(warning|error)\(\s*"' + re.escape(m) + '"', SRC), m


def test_required_alerts_exist() -> None:
    for needle in (
        'resource "google_monitoring_uptime_check_config" "healthz"',
        'path           = "/healthz"',
        'resource "google_monitoring_alert_policy" "error_ratio"',
        'resource "google_monitoring_alert_policy" "latency_p95"',
        'resource "google_monitoring_alert_policy" "saturation"',
        'resource "google_monitoring_slo" "availability"',
        "select_slo_burn_rate",
        'resource "google_monitoring_alert_policy" "cloudsql"',
        'resource "google_monitoring_notification_channel" "email"',
    ):
        assert needle in TF, needle
    # Multi-window: each burn-rate policy ANDs a long and a short window.
    assert 'combiner              = "AND"' in TF and 'short = "300s"' in TF
