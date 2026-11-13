"""Independent at-least-once receipts for MQTT, syslog and permission-checked Slack."""
from datetime import datetime, timedelta, timezone
import json
import os
import socket
import ssl
from uuid import UUID

from .privacy import timestamp
from .state import stamp


def syslog_message(event):
    # No raw record or question; structured JSON is bounded and newline escaped.
    public = {k: event[k] for k in ("event_id", "collection", "rule", "field", "severity", "detected_at", "schema_version",
                                   "rule_version", "rails_commit", "contract_hash", "extractor_version", "build_time", "operation") if k in event}
    payload = json.dumps(public, ensure_ascii=True, separators=(",", ":"))
    if len(payload.encode()) > 8192:
        raise ValueError("Syslog anomaly exceeds event size budget")
    severity = 3 if event.get("severity") == "error" else 4 if event.get("severity") == "warning" else 6
    return f"<{16 * 8 + severity}>1 {stamp()} ledger-anomaly ledger-anomaly - ANOMALY - {payload}".encode()


def send_syslog(event, env=None):
    e = os.environ if env is None else env
    host = e.get("ANOMALY_SYSLOG_HOST")
    if not host:
        raise RuntimeError("ANOMALY_SYSLOG_HOST is not configured")
    transport = e.get("ANOMALY_SYSLOG_TRANSPORT", "tls")
    if transport not in {"tls", "tcp", "udp"}:
        raise ValueError("Unsupported syslog transport")
    port = int(e.get("ANOMALY_SYSLOG_PORT", "6514" if transport == "tls" else "514"))
    message = syslog_message(event)
    if transport == "udp":
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(5)
            client.sendto(message, (host, port))
    else:
        with socket.create_connection((host, port), timeout=5) as client:
            framed = str(len(message)).encode() + b" " + message
            if transport == "tls":
                context = ssl.create_default_context(cafile=e.get("ANOMALY_SYSLOG_CA_FILE"))
                with context.wrap_socket(client, server_hostname=host) as secure:
                    secure.sendall(framed)
            else:
                client.sendall(framed)


class Delivery:
    def __init__(self, state, mqtt=None, slack=None, access=None):
        self.state, self.mqtt, self.slack, self.access = state, mqtt, slack, access

    def step(self, limit=20):
        count = 0
        for key, job in self.state.rows("deliveries"):
            if count >= limit:
                break
            if job["status"] != "pending" or timestamp(job["available_at"]) > datetime.now(timezone.utc):
                continue
            count += 1
            try:
                event = job["event"]
                if job["sink"] == "mqtt":
                    if self.mqtt is None:
                        raise RuntimeError("MQTT unavailable")
                    result = self.mqtt.publish("ledger/v1/anomalies", json.dumps(event, separators=(",", ":")), qos=1, retain=False)
                    result.wait_for_publish(timeout=5)
                    if result.rc != 0 or not result.is_published():
                        raise RuntimeError("MQTT acknowledgment missing")
                elif job["sink"] == "syslog":
                    send_syslog(event)
                elif job["sink"] == "slack":
                    # Channel verification is repeated on every attempt. No member IDs in alerts.
                    channel = self.access.channel_id()
                    if channel != job["channel"]:
                        job.update(status="cancelled", error="channel_changed")
                        self.state.put("deliveries", key, job)
                        continue
                    now = datetime.now(timezone.utc)
                    hour = now.strftime("%Y-%m-%dT%H")
                    sent = self.state.get("slack_sent_budgets", hour, {"count": 0})
                    if sent["count"] >= 10:
                        job["available_at"] = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).isoformat()
                        self.state.put("deliveries", key, job)
                        continue
                    self.slack.chat_postMessage(channel=channel, text=event["text"],
                        client_msg_id=str(UUID(event["event_id"][:32])), unfurl_links=False, unfurl_media=False)
                    sent["count"] += 1
                    self.state.put("slack_sent_budgets", hour, sent)
                else:
                    raise ValueError("Unknown delivery sink")
                job.update(status="delivered", delivered_at=stamp())
            except Exception as exc:
                job["attempts"] += 1
                job.update(error=type(exc).__name__, available_at=(datetime.now(timezone.utc) + timedelta(seconds=min(3600, 2 ** min(job["attempts"], 12)))).isoformat())
            self.state.put("deliveries", key, job)
        return count

    def schedule_alerts(self, findings, config):
        if config.slack_alert_mode != "active" or not self.access:
            return
        now = datetime.now(timezone.utc)
        grouped = {}
        # Durable findings survive a channel lookup failure after detector evaluation.
        for key, finding in self.state.rows("detector_findings"):
            detector = self.state.get("detectors", finding["detector"], {})
            if finding["status"] == "active" and detector.get("status") == "active" and not finding.get("notification"):
                if timestamp(finding["at"]) < now - timedelta(days=1):
                    finding["notification"] = "expired"
                    self.state.put("detector_findings", key, finding)
                else:
                    grouped.setdefault(finding["detector"], []).append(finding)
        if not grouped:
            return
        channel = self.access.channel_id()
        for detector, rows in grouped.items():
            members = {r["member_id"] for r in rows}
            if len(members) < config.minimum_cohort:
                continue
            with self.state.atomic():
                eligible = [r for r in rows if not self.state.get("alert_cooldowns", detector + ":" + r["member_id"]) or
                            timestamp(self.state.get("alert_cooldowns", detector + ":" + r["member_id"])["at"]) <= now - timedelta(hours=24)]
                if len({r["member_id"] for r in eligible}) < config.minimum_cohort:
                    continue
                hour = now.strftime("%Y-%m-%dT%H")
                budget = self.state.get("alert_budgets", hour, {"count": 0, "overflow": 0})
                if budget["count"] >= 9:
                    budget["overflow"] += len(eligible)
                    self.state.put("alert_budgets", hour, budget)
                    # One deterministic summary per hour is updated before delivery, capped at ten notices.
                    key = "overflow:" + hour
                    pending = self.state.get("deliveries", key)
                    if not pending or pending["status"] == "pending":
                        self.state.put("deliveries", key, {"sink": "slack", "channel": channel, "status": "pending", "attempts": 0,
                            "available_at": (now.replace(minute=59, second=59)).isoformat(),
                            "event": {"event_id": __import__('hashlib').sha256(key.encode()).hexdigest(),
                                      "text": "Additional anomaly alerts were grouped this hour. Review the analytics dashboard for the complete findings."}})
                    for row in eligible:
                        row["notification"] = key
                        self.state.put("detector_findings", row["id"], row)
                        self.state.put("alert_cooldowns", detector + ":" + row["member_id"], {"at": now.isoformat()})
                    continue
                event_id = __import__('hashlib').sha256((detector + hour).encode()).hexdigest()
                self.state.put("deliveries", event_id + ":slack", {"sink": "slack", "channel": channel, "status": "pending", "attempts": 0,
                    "available_at": stamp(), "event": {"event_id": event_id,
                    "text": f"Reviewed detector {detector[:12]} found a significant pattern affecting {len({r['member_id'] for r in eligible})} members. This is an association requiring human review; use /ledger-anomaly for evidence and charts."}})
                budget["count"] += 1
                self.state.put("alert_budgets", hour, budget)
                for row in eligible:
                    row["notification"] = event_id + ":slack"
                    self.state.put("detector_findings", row["id"], row)
                    self.state.put("alert_cooldowns", detector + ":" + row["member_id"], {"at": now.isoformat()})
