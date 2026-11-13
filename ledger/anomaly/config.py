"""Explicit configuration; no credential values belong in diagnostics."""
from dataclasses import dataclass
import os
import json
import re
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    database_url: str
    source_uri: str
    source_database: str
    schema_path: str
    timezone: str = "America/New_York"
    minimum_cohort: int = 5
    grace_days: int = 30
    slack_alert_mode: str = "shadow"
    event_days: int = 90
    history_months: int = 60
    batch_size: int = 500
    reconcile_seconds: int = 780
    reference_delay: int = 60
    security_enabled: bool = False
    security_alert_mode: str = "active"
    security_dm_user_ids: tuple = ()
    security_rules_path: str = str(Path(__file__).with_name("security_rules.json"))
    security_evaluation_seconds: int = 15

    @classmethod
    def from_env(cls, env=None):
        e = os.environ if env is None else env
        result = cls(e.get("ANOMALY_DATABASE_URL", ""), e.get("MLAB_URI", ""),
                     e.get("MLAB_DATABASE", ""), e.get("ANOMALY_SCHEMA_PATH", "/app/schema/manifest.json"),
                     e.get("ANOMALY_TIMEZONE", "America/New_York"),
                     int(e.get("ANOMALY_MIN_COHORT_SIZE", "5")), int(e.get("ANOMALY_LAPSE_GRACE_DAYS", "30")),
                     e.get("ANOMALY_SLACK_ALERT_MODE", "shadow"), int(e.get("ANOMALY_EVENT_RETENTION_DAYS", "90")),
                     int(e.get("ANOMALY_HISTORY_MONTHS", "60")), int(e.get("ANOMALY_BATCH_SIZE", "500")),
                     int(e.get("ANOMALY_RECONCILE_SECONDS", "780")), int(e.get("ANOMALY_REFERENCE_DELAY", "60")),
                     e.get("ANOMALY_SECURITY_ENABLED", "false").strip().lower() == "true",
                     e.get("ANOMALY_SECURITY_ALERT_MODE", "active"),
                     cls.security_recipients(e.get("ANOMALY_SECURITY_DM_USER_IDS", "[]")),
                     e.get("ANOMALY_SECURITY_RULES_PATH") or cls.security_rules_path,
                     int(e.get("ANOMALY_SECURITY_EVALUATION_SECONDS", "15")))
        if urlparse(result.database_url).scheme not in {"postgres", "postgresql"}:
            raise ValueError("ANOMALY_DATABASE_URL must name PostgreSQL")
        if not result.source_uri.startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError("MLAB_URI must name the read-only Mongo source")
        if result.minimum_cohort < 5 or result.grace_days < 0 or not 1 <= result.batch_size <= 5000:
            raise ValueError("Invalid cohort, grace period, or batch size")
        if min(result.event_days, result.history_months, result.reconcile_seconds, result.reference_delay) < 1:
            raise ValueError("Retention and polling intervals must be positive")
        if result.slack_alert_mode not in {"shadow", "active"}:
            raise ValueError("ANOMALY_SLACK_ALERT_MODE must be shadow or active")
        if result.security_alert_mode not in {"shadow", "active"} or not 1 <= result.security_evaluation_seconds <= 300:
            raise ValueError("Invalid security alert mode or evaluation interval")
        if result.security_enabled:
            from .security_learning import load_rules
            load_rules(result.security_rules_path)
        ZoneInfo(result.timezone)
        if not Path(result.schema_path).is_file():
            raise ValueError("A freshly built ANOMALY_SCHEMA_PATH is required")
        return result

    @staticmethod
    def security_recipients(raw):
        try:
            values = json.loads(raw)
        except (ValueError, TypeError):
            raise ValueError("ANOMALY_SECURITY_DM_USER_IDS must be a JSON array of Slack user IDs") from None
        if not isinstance(values, list) or len(values) > 100 or any(not isinstance(v, str) or not re.fullmatch(r'[UW][A-Z0-9]+', v) for v in values):
            raise ValueError("ANOMALY_SECURITY_DM_USER_IDS must contain at most 100 Slack user IDs")
        return tuple(sorted(set(values)))
