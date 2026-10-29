"""Independent analytics process; external outages never authorize source mutations."""
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from threading import Thread
import time

from .analytics import Analytics
from .delivery import Delivery
from .detectors import DetectorRegistry
from .evidence import reconstruct_coverage
from .ingest import Ingestor
from .privacy import timestamp
from .reports import Reports
from .schema import SchemaRegistry
from .state import stamp
from .security import SecurityEngine
from .security_delivery import SecurityDelivery
from .security_evidence import enabled as security_enabled

log = logging.getLogger(__name__)


def broker(ingestor):
    import paho.mqtt.client as mqtt
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=os.environ.get("MQTT_CLIENT_ID", "ledger-anomaly"), clean_session=False)
    if os.environ.get("MQTT_USER"):
        client.username_pw_set(os.environ["MQTT_USER"], os.environ.get("MQTT_PASSWORD"))
    if os.environ.get("MQTT_TLS", "false").lower() == "true":
        client.tls_set()
    def connected(c, userdata, flags, code, properties):
        if not code.is_failure:
            c.subscribe([(name + "/+", 1) for name in ingestor.registry.contracts()])
    def message(c, userdata, msg):
        # Legacy packets have no replay IDs; body is deliberately not inspected or stored.
        if not msg.retain and msg.topic.split("/", 1)[0] in ingestor.registry.contracts():
            ingestor.state.put("reconcile", "all", {"requested_at": stamp(), "source": "mqtt_hint"})
    client.on_connect, client.on_message = connected, message
    client.reconnect_delay_set(1, 60)
    client.connect_async(os.environ.get("MQTT_HOST", "localhost"), int(os.environ.get("MQTT_PORT", "1883")), 60)
    client.loop_start()
    return client


class Service:
    def __init__(self, state, source, manifest, config):
        self.state, self.source, self.config = state, source, config
        self.registry = SchemaRegistry(state, manifest)
        self.registry.install()
        self.ingestor = Ingestor(state, source, self.registry, config)
        self.analytics = Analytics(state, config)
        self.detectors = DetectorRegistry(state)
        self.reports = None
        self.ledger = None
        self.delivery = Delivery(state)
        self.security = SecurityEngine(state, config)
        self.security_delivery = SecurityDelivery(state, config)
        self.security_thread = None
        self.scan_thread = None
        self.analysis_thread = None
        self.io_thread = Thread(target=self.external_io, daemon=True)

    def connect_slack(self):
        if not os.environ.get("SLACK_BOT_TOKEN") or not os.environ.get("LEDGER_URI"):
            self.state.put("health", "slack", {"status": "unconfigured", "updated_at": stamp()})
            return
        from ..cli import database_ledger
        from ..messages import ChatAPI, DEFAULT_MODEL
        from ..slack_client import SlackCallDebugClient
        from .slack import AnomalyAccess
        from .metabase import MetabaseClient
        self.ledger = database_ledger()
        slack = SlackCallDebugClient(token=os.environ["SLACK_BOT_TOKEN"], timeout=10, retry_handlers=[])
        access = AnomalyAccess(self.ledger, slack)
        api = None
        key = os.environ.get("ANOMALY_LLM_API_KEY") or os.environ.get("LEDGER_LLM_API_KEY", "")
        if key or os.environ.get("ANOMALY_LLM_BASE_URL"):
            api = ChatAPI(os.environ.get("ANOMALY_LLM_BASE_URL") or os.environ.get("LEDGER_LLM_BASE_URL", "http://ledger-ai:8000/v1"),
                          os.environ.get("ANOMALY_LLM_MODEL") or os.environ.get("LEDGER_LLM_MODEL", DEFAULT_MODEL), key, deadline=30)
        metabase = None
        if os.environ.get("ANOMALY_METABASE_URL") and os.environ.get("ANOMALY_METABASE_CHAT_API_KEY"):
            metabase = MetabaseClient(os.environ["ANOMALY_METABASE_URL"], os.environ["ANOMALY_METABASE_CHAT_API_KEY"],
                                      os.environ.get("ANOMALY_METABASE_CHART_API_KEY", ""))
        self.reports = Reports(self.state, self.ledger, slack, access, self.registry, self.config, api, metabase)
        self.delivery.slack, self.delivery.access = slack, access
        self.security_delivery.slack, self.security_delivery.access = slack, access
        self.state.put("health", "slack", {"status": "ready", "updated_at": stamp()})

    def job(self):
        from ..domain import Denied
        if self.reports is None:
            return
        job = self.ledger.store.claim("ledger_outbox", kinds=["anomaly_query"])
        if not job:
            return
        status, delay, error = "done", 0, None
        try:
            self.reports.deliver(job)
        except Denied:
            status, error = "cancelled", "Denied"
            with self.state.atomic():
                self.state.delete("query_results", job["_id"])
                receipt = self.state.get("query_receipts", job["_id"], {})
                receipt.update(completed_at=stamp(), status="cancelled")
                self.state.put("query_receipts", job["_id"], receipt)
        except Exception as exc:
            status, delay, error = "pending", max(getattr(exc, "retry_after_seconds", 0), min(300, 2 ** min(job["attempts"], 8))), type(exc).__name__
            log.warning("Anomaly request retry error=%s", error)
        def finish(store):
            current = store.get("ledger_outbox", job["_id"])
            if current and current.get("lease") == job["lease"] and current.get("status") == "working":
                current.update(status=status, available_at=datetime.now(timezone.utc) + timedelta(seconds=delay), last_error=error)
                store.put("ledger_outbox", current)
        self.ledger.store.atomic(finish)

    def analyze(self, train=False):
        try:
            if not self.source_current():
                self.state.put("health", "analysis", {"status": "catching_up", "updated_at": stamp()})
                return
            reconstruct_coverage(self.state, history_months=self.config.history_months)
            features = self.analytics.refresh()
            findings = self.detectors.evaluate(features)
            if train:
                self.analytics.train()
            self.analytics.accept_baselines()
            self.delivery.schedule_alerts(findings, self.config)
            self.state.put("health", "analysis", {"status": "ready", "updated_at": stamp()})
        except Exception as exc:
            self.state.put("health", "analysis", {"status": "error", "error": type(exc).__name__, "updated_at": stamp()})
            log.warning("Anomaly analysis failed error=%s", type(exc).__name__)

    def source_current(self):
        if self.state.pending(1):
            return False
        now = datetime.now(timezone.utc)
        for collection in self.registry.contracts():
            health = self.state.get("health", "stream:" + collection, {})
            updated = timestamp(health.get("updated_at"))
            if health.get("status") != "ready" or health.get("caught_up") is not True or not updated or now - updated > timedelta(seconds=30):
                return False
        return True

    def scan(self, historical):
        try:
            self.ingestor.scan(historical=historical)
        except Exception as exc:
            self.state.put("health", "scan", {"status": "error", "error": type(exc).__name__, "updated_at": stamp()})
            log.warning("Anomaly reconciliation failed error=%s", type(exc).__name__)

    def run(self):
        self.connect_slack()
        self.delivery.mqtt = broker(self.ingestor)
        self.ingestor.start()
        initial = not self.state.get("health", "bootstrap")
        self.scan_thread = Thread(target=self.scan, args=(initial,), daemon=True)
        self.scan_thread.start()
        last_scan = time.monotonic()
        last_analysis = last_train = last_prune = last_security = 0
        try:
            while not self.ingestor.stop.is_set():
                self.state.put("health", "runtime", {"status": "ready", "updated_at": stamp()})
                processed = self.ingestor.drain()
                self.ingestor.check_references()
                if initial and not self.scan_thread.is_alive() and self.source_current():
                    if self.state.get("health", "scan", {}).get("status") == "ready":
                        self.state.put("health", "bootstrap", {"status": "ready", "completed_at": stamp(), "updated_at": stamp()})
                        initial = False
                now = time.monotonic()
                if security_enabled(self.config) and now-last_security >= self.config.security_evaluation_seconds and (self.security_thread is None or not self.security_thread.is_alive()):
                    self.security_thread = Thread(target=self.analyze_security, daemon=True)
                    self.security_thread.start()
                    last_security = now
                if not self.scan_thread.is_alive() and (now - last_scan >= self.config.reconcile_seconds or
                        now - last_scan >= 60 and self.state.get("reconcile", "all")):
                    self.scan_thread = Thread(target=self.scan, args=(initial,), daemon=True)
                    self.scan_thread.start()
                    last_scan = now
                if not initial and now - last_analysis >= 3600 and self.source_current() and (self.analysis_thread is None or not self.analysis_thread.is_alive()):
                    self.analysis_thread = Thread(target=self.analyze, args=(now - last_train >= 86400,), daemon=True)
                    self.analysis_thread.start()
                    if now - last_train >= 86400:
                        last_train = now
                    last_analysis = now
                # Slow enrichment/upload and external sink retries must not stop CDC processing.
                if not self.io_thread.is_alive():
                    self.io_thread = Thread(target=self.external_io, daemon=True)
                    self.io_thread.start()
                if now - last_prune >= 86400:
                    self.state.prune(self.config.event_days, self.config.history_months)
                    last_prune = now
                if not processed:
                    self.ingestor.stop.wait(.2)
        finally:
            self.ingestor.stop.set()
            self.delivery.mqtt.loop_stop()
            self.delivery.mqtt.disconnect()
            for thread in (self.scan_thread, self.analysis_thread, self.security_thread, self.io_thread):
                if thread and thread.ident is not None:
                    thread.join(timeout=5)
            self.state.put("health", "runtime", {"status": "stopped", "updated_at": stamp()})

    def external_io(self):
        for component, action in [('slack_jobs', self.job), ('delivery', self.delivery.step),
                                  ('security_delivery', self.security_delivery.step)]:
            try:
                action()
            except Exception as exc:
                self.state.put('health', component, {'status': 'error', 'error': type(exc).__name__, 'updated_at': stamp()})

    def analyze_security(self):
        try:
            self.security.evaluate()
        except Exception as exc:
            self.state.put('health', 'security', {'status': 'error', 'error': type(exc).__name__, 'updated_at': stamp()})


def readiness(state, source, max_age=180):
    state.ready()
    source.command("ping")
    health = state.get("health", "runtime", {})
    if health.get("status") != "ready" or not timestamp(health.get("updated_at")) or datetime.now(timezone.utc) - timestamp(health["updated_at"]) > timedelta(seconds=max_age):
        raise RuntimeError("Analytics runtime heartbeat is stale")
    for component, stream in state.rows("health"):
        if component.startswith("stream:") and (stream.get("status") != "ready" or not timestamp(stream.get("updated_at")) or
                datetime.now(timezone.utc) - timestamp(stream["updated_at"]) > timedelta(seconds=max_age)):
            raise RuntimeError("A source stream is unavailable or stale")
    # Baseline warm-up is observable but does not force Docker to kill a healthy backfill.
    return {"status": "ready", "bootstrap": state.get("health", "bootstrap", {}).get("status", "warming"),
            "schema": state.get("health", "schema", {})}
