"""Runtime boundaries and durable state behavior, without external credentials."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
pytest.importorskip("dateutil")

from ledger.anomaly.config import Config
from ledger.anomaly.delivery import Delivery, syslog_message
from ledger.anomaly.detectors import DetectorRegistry, validate_spec
from ledger.anomaly.privacy import normalize, aggregate_rows, timestamp
from ledger.anomaly.reports import Reports, request_kind, AttachmentPending
from ledger.anomaly.schema import SchemaRegistry, compatibility, validate_document, load_manifest
from ledger.anomaly.state import MemoryState
from ledger.anomaly.service import readiness, Service
from ledger.domain import Denied


def manifest():
    return {"format_version": 1, "provenance": {"rails_commit": "a" * 40}, "collections": {
        "members": {"fields": {"status": {"types": ["String"], "enum": ["activeMember", "revoked"]}}, "required": ["status"]}}}


def proposal():
    return {"title": "Reduced recent attendance", "feature": "visit_ratio", "family": "lower_threshold", "threshold": .3,
            "window_days": 90, "minimum_evidence": 50, "comparison_group": "member_history", "reference": "Advanced Techniques for Anomaly Detection",
            "confounders": "Seasonal closures and incomplete attendance", "falsification": "No improvement over recency after temporal holdout",
            "expected_alert_rate": .05}


def test_atomic_checkpoint_and_event_rollback_and_replay():
    state = MemoryState()
    with pytest.raises(RuntimeError):
        with state.atomic():
            state.capture("e", "members", {"safe": True}, {"token": 1})
            raise RuntimeError("crash")
    assert state.checkpoint("members") is None
    assert state.pending() == []
    assert state.capture("e", "members", {"safe": True}, {"token": 1})
    assert not state.capture("e", "members", {"safe": True}, {"token": 1})
    assert len(state.pending()) == 1
    with pytest.raises(RuntimeError):
        with state.atomic():
            state.put("baselines", "member", {"n": 1})
            state.processed("e")
            raise RuntimeError("crash")
    assert state.get("baselines", "member") is None
    assert len(state.pending()) == 1


def test_projection_excludes_private_fields_and_raw_identifiers():
    private = {"encrypted_password": "secret", "notes": "private", "email": "private@example.org", "amount": 99, "transaction_id": "tx-secret"}
    data = normalize("members", {**private, "status": "activeMember", "subscription": True}, "key")
    assert data == {"status": "activeMember", "subscription": True}
    assert normalize("notes", {"body": "secret"}, "key") == {}
    card = normalize("cards", {"uid": "access-secret", "member_id": "member1"}, "key")
    assert card["member_id"] == "member1"
    assert "access-secret" not in json.dumps(card)
    invoice = normalize("invoices", {**private, "resource_class": "member"}, "key")
    assert invoice["paid_positive"] is True
    assert "tx-secret" not in json.dumps(invoice) and "99" not in json.dumps(invoice)


def test_safe_audit_billing_discriminator_and_null_initial_expiry():
    row = normalize("audit_logs", {"after_snapshot": {"resource_class": "rental", "resource_id": "123", "notes": "secret"},
        "field_changes": {"expirationTime": [None, 1800000000000], "password": ["a", "b"]}}, "key")
    assert row["resource_class"] == "rental"
    assert row["field_changes"]["expirationTime"][0] is None
    assert "secret" not in json.dumps(row)


def test_small_bucket_suppresses_entire_dimension_not_only_cell():
    rows = [{"report": "r", "dimension": "city", "bucket": x, "sample_size": n} for x, n in [("a", 20), ("b", 3)]]
    assert aggregate_rows(rows) == []
    rows[-1]["sample_size"] = 5
    assert len(aggregate_rows(rows)) == 2
    with pytest.raises(ValueError): aggregate_rows(rows, 1)


def test_mixed_timestamp_units_invalid_and_dst_offsets():
    assert timestamp(1800000000) == timestamp(1800000000000)
    assert timestamp("2026-11-01T01:30:00-04:00") != timestamp("2026-11-01T01:30:00-05:00")
    assert timestamp(float("nan")) is None


def test_malformed_source_enums_do_not_stop_structural_monitoring():
    assert normalize("members", {"status": {"bad": True}}, "key") == {}
    assert normalize("audit_logs", {"event_type": [], "after_snapshot": {"resource_class": []},
        "field_changes": {"status": [[], {}]}}, "key") == {"field_changes": {}}


def test_schema_new_field_is_drift_not_error_and_no_values_exposed():
    findings = validate_document(manifest()["collections"]["members"], {"status": "private-status", "private-secret-field": "secret"})
    assert {r["rule"] for r in findings} == {"enum", "unknown_field"}
    assert next(r for r in findings if r["rule"] == "unknown_field")["severity"] == "info"
    assert "secret" not in json.dumps(findings) and "private-status" not in json.dumps(findings)


def test_schema_compatible_update_and_reviewed_incompatible_revision():
    state = MemoryState()
    registry = SchemaRegistry(state, manifest())
    registry.install()
    later = manifest()
    later["collections"]["members"]["fields"]["new_optional"] = {"types": ["String"]}
    added = SchemaRegistry(state, later)
    assert added.install()["pending"] == {}
    later["collections"]["members"]["required"].append("new_optional")
    changed = SchemaRegistry(state, later)
    assert changed.install()["pending"]["members"] == ["required_added"]
    assert changed.contracts()["members"]["required"] == ["status"]
    with pytest.raises(ValueError): changed.approve("stale", "admin")
    changed.approve(changed.version, "admin")
    assert "new_optional" in changed.contracts()["members"]["required"]


def test_actual_system_configs_collection_is_allowed(tmp_path):
    value = manifest()
    value["collections"]["system_configs"] = {"fields": {}, "required": []}
    file = tmp_path / "manifest.json"
    file.write_text(json.dumps(value))
    assert "system_configs" in load_manifest(file)["collections"]


@pytest.mark.parametrize("field,value", [("feature", "password"), ("family", "eval"), ("threshold", float("nan")),
    ("expected_alert_rate", .9), ("window_days", 10000), ("title", "<@user>")])
def test_detector_proposals_are_constrained(field, value):
    spec = proposal()
    spec[field] = value
    with pytest.raises(ValueError): validate_spec(spec)


def test_detector_cannot_activate_without_shadow_evidence():
    state = MemoryState()
    registry = DetectorRegistry(state)
    version = registry.propose(proposal(), "admin")
    assert state.get("detectors", version)["status"] == "shadow"
    with pytest.raises(ValueError): registry.approve(version, "admin")
    registry.disable(version, "admin")
    assert state.get("detectors", version)["status"] == "disabled"


def test_feature_semantics_change_requires_new_detector_review(monkeypatch):
    state = MemoryState()
    registry = DetectorRegistry(state)
    old = registry.propose(proposal(), "admin")
    monkeypatch.setattr("ledger.anomaly.detectors.FEATURE_VERSION", "new semantics")
    new = registry.propose(proposal(), "admin")
    assert old != new and state.get("detectors", new)["status"] == "shadow"
    with pytest.raises(ValueError): registry.approve(old, "admin")


def test_only_staff_dm_can_resolve_learning_quarantine():
    reports, state, access, slack, job = report_fixture()
    job["payload"]["text"] = "detectors resolve finding1"
    state.put("detector_findings", "finding1", {"status": "shadow", "member_id": "m"})
    with pytest.raises(Denied): reports.build(job)
    access.authorize.return_value = "staff_detail"
    access.staff.return_value = True
    assert "resolved" in reports.build(job)["text"]
    assert state.get("detector_findings", "finding1")["resolved_by"] == "U1"


def test_each_sink_has_independent_delivery_receipt(monkeypatch):
    state = MemoryState()
    event = {"event_id": "id", "collection": "members", "rule": "type", "severity": "error"}
    for sink in ("mqtt", "syslog"):
        state.put("deliveries", sink, {"sink": sink, "event": event, "status": "pending", "attempts": 0, "available_at": "2020-01-01T00:00:00+00:00"})
    mqtt = Mock()
    mqtt.publish.return_value.rc = 0
    mqtt.publish.return_value.is_published.return_value = True
    monkeypatch.setattr("ledger.anomaly.delivery.send_syslog", Mock(side_effect=OSError()))
    Delivery(state, mqtt=mqtt).step()
    assert state.get("deliveries", "mqtt")["status"] == "delivered"
    assert state.get("deliveries", "syslog")["status"] == "pending"
    assert mqtt.publish.call_args.kwargs == {"qos": 1, "retain": False}
    assert b"private" not in syslog_message({**event, "private": "private"})


def report_fixture():
    state = MemoryState()
    registry = SchemaRegistry(state, manifest())
    registry.install()
    access, slack, ledger = Mock(), Mock(), Mock()
    access.authorize.return_value = "aggregate"
    reports = Reports(state, ledger, slack, access, registry, SimpleNamespace(minimum_cohort=5))
    job = {"_id": "job1", "payload": {"user_id": "U1", "channel": "C1", "text": "attendance patterns", "thread_ts": None}}
    return reports, state, access, slack, job


def test_queued_authority_change_prevents_delivery():
    reports, state, access, slack, job = report_fixture()
    state.put("query_results", "job1", {"text": "private", "capability": "staff_detail", "charts": []})
    with pytest.raises(Denied): reports.deliver(job)
    slack.chat_postMessage.assert_not_called()


def test_retry_keeps_reserved_report_and_avoids_duplicate_slack_text():
    reports, state, access, slack, job = report_fixture()
    slack.chat_postMessage.return_value = {"ts": "1.2"}
    reports.deliver(job)
    first = state.get("query_results", "job1")
    reports.deliver(job)
    assert state.get("query_results", "job1") == first
    assert slack.chat_postMessage.call_count == 1


def test_metabot_outage_keeps_factual_text_and_seeded_chart():
    reports, state, access, slack, job = report_fixture()
    access.authorize.return_value = "staff_detail"
    state.put("report_metrics", "attendance", {"report": "attendance", "dimension": "window", "bucket": "28",
        "value": 120, "sample_size": 30, "as_of": "2026-10-01T00:00:00+00:00"})
    state.put("metabase", "bootstrap", {"cards": {"attendance": 7}})
    reports.metabase = Mock(base_url="http://curated.example")
    reports.metabase.ask.side_effect = RuntimeError("private provider error")
    reports.metabase.render.side_effect = RuntimeError("renderer unavailable")
    with pytest.raises(AttachmentPending): reports.deliver(job)
    result = state.get("query_results", job["_id"])
    assert "120" in result["text"] and "2026-10-01" in result["text"]
    assert result["charts"] == [{"type": "saved_card", "card_id": 7}]
    assert "private provider" not in result["text"]
    slack.chat_postMessage.assert_called_once()
    slack.files_upload_v2.assert_not_called()
    reports.metabase.render.side_effect = None
    reports.metabase.render.return_value = b"png"
    reports.deliver(job)
    assert slack.chat_postMessage.call_count == 1
    slack.files_upload_v2.assert_called_once()


def test_chart_upload_rechecks_authority_after_render():
    reports, state, access, slack, job = report_fixture()
    state.put("query_results", "job1", {"text": "report", "capability": "aggregate", "charts": [{"type": "saved_card", "card_id": 7}]})
    reports.metabase = Mock()
    def render(ref):
        access.authorize.side_effect = Denied("access removed during rendering")
        return b"png"
    reports.metabase.render.side_effect = render
    with pytest.raises(Denied): reports.deliver(job)
    slack.files_upload_v2.assert_not_called()


@pytest.mark.parametrize("confidence,as_of", [("historical_ownership_uncertain", None), ("verified", "2000-01-01T00:00:00+00:00")])
def test_member_probabilities_require_fresh_verified_features(monkeypatch, confidence, as_of):
    reports, state, access, slack, job = report_fixture()
    access.authorize.return_value = "staff_detail"
    reports.ledger.sources.identity.return_value = {"_id": "m"}
    job["payload"]["text"] = "churn <@U2>"
    state.put("member_analytics", "m", {"features": {}, "payer_type": ["direct"], "sustained": {},
        "confidence": confidence, "as_of": as_of or datetime.now(timezone.utc).isoformat()})
    model = Mock(return_value=.9)
    monkeypatch.setattr("ledger.anomaly.reports.predict", model)
    assert "confidence" in reports.build(job)["text"]
    model.assert_not_called()


def test_alert_schedule_recovers_durable_findings_after_slack_outage():
    state, access = MemoryState(), Mock()
    state.put("detectors", "d", {"status": "active"})
    now = datetime.now(timezone.utc).isoformat()
    for i in range(5):
        state.put("detector_findings", str(i), {"id": str(i), "detector": "d", "member_id": str(i), "at": now, "status": "active"})
    delivery = Delivery(state, access=access)
    access.channel_id.side_effect = OSError("offline")
    config = SimpleNamespace(slack_alert_mode="active", minimum_cohort=5)
    with pytest.raises(OSError): delivery.schedule_alerts([], config)
    access.channel_id.side_effect = None
    access.channel_id.return_value = "C1"
    delivery.schedule_alerts([], config)
    assert len(list(state.rows("deliveries"))) == 1
    assert all(v.get("notification") for _, v in state.rows("detector_findings"))
    delivery.schedule_alerts([], config)
    assert len(list(state.rows("deliveries"))) == 1


def test_slack_delivery_rate_limit_applies_to_old_retried_jobs():
    state, access, slack = MemoryState(), Mock(), Mock()
    access.channel_id.return_value = "C1"
    hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    state.put("slack_sent_budgets", hour, {"count": 10})
    state.put("deliveries", "old", {"sink": "slack", "channel": "C1", "status": "pending", "attempts": 2,
        "event": {"event_id": "a" * 64, "text": "report"}, "available_at": "2020-01-01T00:00:00+00:00"})
    Delivery(state, slack=slack, access=access).step()
    slack.chat_postMessage.assert_not_called()
    assert state.get("deliveries", "old")["status"] == "pending"


def test_prompt_pair_recent_avoidance_and_retry_snapshot():
    reports, state, *_ = report_fixture()
    first = reports.prompt("first", "anomaly_propose", "approved aggregate")
    second = reports.prompt("second", "anomaly_propose", "approved aggregate")
    assert first["choice"] != second["choice"]
    assert reports.prompt("first", "anomaly_propose", "changed") == first
    assert "approved aggregate" in first["prompt"]
    assert "Never write code" in first["system"]


def test_readiness_rejects_stale_runtime_without_modifying_source():
    state, source = MemoryState(), Mock()
    with pytest.raises(RuntimeError): readiness(state, source)
    state.put("health", "runtime", {"status": "ready", "updated_at": datetime.now(timezone.utc).isoformat()})
    assert readiness(state, source)["bootstrap"] == "warming"
    source.command.assert_called_with("ping")


def test_analysis_waits_for_stream_catchup_and_committed_processing():
    state = MemoryState()
    service = Service(state, Mock(), manifest(), SimpleNamespace(minimum_cohort=5, history_months=60,
        timezone="America/New_York", grace_days=30, slack_alert_mode="shadow"))
    assert not service.source_current()
    service.analyze()
    assert state.get("health", "analysis")["status"] == "catching_up"
    state.put("health", "stream:members", {"status": "ready", "caught_up": True, "updated_at": datetime.now(timezone.utc).isoformat()})
    assert service.source_current()
    state.capture("pending", "members", {})
    assert not service.source_current()
    state.processed("pending")
    service.analyze()
    assert state.get("health", "analysis")["status"] == "ready"
