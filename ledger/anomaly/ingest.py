"""Read-only Mongo capture. Resume checkpoints and sanitized events commit together."""
from datetime import datetime, timedelta, timezone
import json
import logging
import re
import secrets
import time
from threading import Event, Thread

from bson import json_util
from pymongo.errors import OperationFailure

from .privacy import ALLOWED, normalize, identifier, digest, timestamp
from .schema import validate_document, fingerprint, canonical
from .state import stamp
from .security_projection import COLLECTIONS as SECURITY_COLLECTIONS, project as security_project
from .security_evidence import configure as configure_security, enabled as security_enabled, record as record_security

log = logging.getLogger(__name__)


def encoded(value):
    return json.loads(json_util.dumps(value))


def decoded(value):
    return json_util.loads(json.dumps(value))


MISSING = object()


def field_value(document, path):
    """Read a Mongo stored path, including multikey arrays, without Ruby aliases."""
    parts = path.split(".")
    def descend(value, remaining):
        if not remaining:
            return value
        if isinstance(value, list):
            values = [descend(item, remaining) for item in value]
            return [item for value in values if value is not MISSING for item in (value if isinstance(value, list) else [value])]
        if not isinstance(value, dict) or remaining[0] not in value:
            return MISSING
        return descend(value[remaining[0]], remaining[1:])
    return descend(document, parts)


def verify_source(source, manifest):
    """Exercise actual per-collection privileges without creating/modifying anything."""
    source.command("ping")
    failures = []
    for name in sorted(manifest["collections"]):
        try:
            source[name].find_one({}, {"_id": 1}, max_time_ms=3000)
            with source[name].watch(max_await_time_ms=100, batch_size=1) as stream:
                stream.try_next()
        except Exception:
            failures.append(name)
    if failures:
        raise PermissionError("Read/changeStream preflight failed for: " + ", ".join(failures))


class Ingestor:
    def __init__(self, state, source, registry, config):
        self.state, self.source, self.registry, self.config = state, source, registry, config
        self.stop = Event()
        self.threads = []
        self.started = {}
        with state.atomic():
            self.secret = state.get("config", "pseudonym_key", {}).get("value") or secrets.token_hex(32)
            state.put("config", "pseudonym_key", {"value": self.secret})
        configure_security(state, config)

    def inspection(self, collection, doc, enforced_required=()):
        schema = self.registry.contracts().get(collection)
        if schema is None:
            return []  # A concurrent approval can retire a collection mid-read.
        newly_required = self.registry.required_since(collection)
        findings = [finding for finding in validate_document(schema, doc)
                    if finding["rule"] != "required" or finding["field"] not in newly_required
                    or finding["field"] in enforced_required]
        # Values are used only in bounded source queries, never saved with a finding.
        for index in schema.get("indexes", []):
            keys = index.get("keys", {})
            if not index.get("unique") or not keys:
                continue
            values = {key: field_value(doc, key) for key in keys}
            # A sparse compound index includes a document when ANY indexed path exists.
            if index.get("sparse") and all(value is MISSING for value in values.values()):
                continue
            if any(isinstance(value, list) and (not value or len(value) > 1000) for value in values.values()):
                findings.append({"rule": "unique_index_coverage", "field": ",".join(sorted(keys)), "severity": "warning"})
                continue
            query = {key: None if value is MISSING else {"$in": value} if isinstance(value, list) else value
                     for key, value in values.items()}
            if index.get("sparse"):
                query = {"$and": [query, {"$or": [{key: {"$exists": True}} for key in keys]}]}
            options = {"max_time_ms": 2000}
            if index.get("collation"):
                options["collation"] = index["collation"]
            partial = index.get("partial_filter_expression") or index.get("partialFilterExpression")
            if partial:
                query = {"$and": [query, partial]}
                if not self.source[collection].find_one({"$and": [{"_id": doc["_id"]}, partial]}, {"_id": 1}, **options):
                    continue
            query = {"$and": [query, {"_id": {"$ne": doc.get("_id")}}]}
            if self.source[collection].find_one(query, {"_id": 1}, **options):
                findings.append({"rule": "duplicate", "field": ",".join(sorted(keys)), "severity": "error"})
        return findings

    def unchanged_snapshot(self, key, signature, data, dependencies):
        """Reuse the receipt while preserving the latest successful read boundary.

        Called inside the capture transaction, including before event processing.
        An unchanged scan still proves that older buffered updates are stale.
        """
        receipt = self.state.get("capture_receipts", key, {})
        if receipt.get("signature") != signature or receipt.get("stale"):
            return None
        read_started_at, observed_at = data["read_started_at"], data["observed_at"]
        updated = False
        if timestamp(read_started_at) > timestamp(receipt["read_started_at"]):
            receipt.update(read_started_at=read_started_at, observed_at=observed_at)
            if data["operation"] == "snapshot":
                receipt["snapshot_read"] = {"event_id": receipt["event_id"],
                    "read_started_at": read_started_at, "observed_at": observed_at}
            updated = True
            old = self.state.get("observations", key, {})
            if old.get("capture_event_id") == receipt["event_id"] and old.get("operation") == "snapshot":
                self.state.put("observations", key, {**old, "read_started_at": read_started_at, "observed_at": observed_at})
        if receipt.get("dependencies") != dependencies and data["operation"] == "snapshot":
            # Target changes need delayed cross-document validation, not another
            # retained copy of this unchanged document.
            receipt["dependencies"] = dependencies
            updated = True
            self.state.put("reference_checks", key, {"collection": key.split(":", 1)[0],
                "document_key": data["document_key"], "required_enforced": data["required_enforced"],
                "due": (timestamp(observed_at) + timedelta(seconds=self.config.reference_delay)).isoformat(),
                "historical": data["historical"], "generation": fingerprint([receipt["event_id"], dependencies])})
        if updated:
            self.state.put("capture_receipts", key, receipt)
        return receipt["event_id"]

    def capture(self, collection, doc, operation, token=None, *, document_id=None, historical=False, transition=None,
                generation=None, removed=None, occurred_at=None, source_order=None, read_started_at=None, reconciled=False,
                changed_fields=(), updated_fields=None, security_removed=()):
        schema = self.registry.contracts().get(collection)
        if schema is None:
            return None
        observed_at = stamp()
        doc_id = doc.get("_id") if doc else document_id
        # Internal IDs allow a source re-read; opaque custom IDs remain BSON encoded.
        key = canonical(encoded(doc_id))
        values = normalize(collection, doc or {}, self.secret)
        security = (security_project(collection, doc, self.secret, operation=operation,
                    updates=updated_fields, removed=security_removed)
                    if security_enabled(self.config) and collection in SECURITY_COLLECTIONS else None)
        entity = identifier(doc_id) or fingerprint(encoded(doc_id))
        old = self.state.get("observations", collection + ":" + entity, {})
        enforced = set(old.get("required_enforced", []))
        event_at = timestamp(occurred_at or observed_at)
        changes = [re.sub(r"\.\d+(?=\.|$)", "[]", path) for path in changed_fields if isinstance(path, str)]
        for field, accepted_at in self.registry.required_since(collection).items():
            changed = any(field == path or field.startswith(path + ".") or field.startswith(path + "[].") for path in changes)
            if event_at and event_at > timestamp(accepted_at) and (operation in {"insert", "replace"} or operation == "update" and changed):
                enforced.add(field)
        # Hash the whole source document with the local secret: structural changes
        # outside the analytics projection still matter, but their values stay private.
        document_fingerprint = digest(canonical(encoded(doc)), self.secret)
        targets = {ref.get("collection") for ref in schema.get("references", [])} - {None}
        if collection == "members" and "groups" in self.registry.contracts():
            targets.add("groups")
        if any(index.get("unique") for index in schema.get("indexes", [])):
            targets.add(collection)
        dependencies = {target: self.state.get("source_revisions", target) for target in sorted(targets)}
        signature = fingerprint([collection, key, operation, document_fingerprint, values,
                                 self.registry.version, schema, sorted(enforced), security])
        observation_key = collection + ":" + entity
        payload = {"document_key": encoded(doc_id), "entity": key, "operation": operation, "values": values,
                   "observed_at": observed_at, "read_started_at": read_started_at or observed_at,
                   "source_order": source_order, "reconciled": reconciled, "historical": historical,
                   "required_enforced": sorted(enforced), "provenance": self.registry.provenance(),
                   "schema_version": self.registry.version, "transition": transition or {}, "removed": removed or [],
                   "missing_document": doc is None and operation != "delete", "occurred_at": occurred_at,
                   "document_fingerprint": document_fingerprint}
        if security is not None:
            payload['security'] = security
        if token is None:
            with self.state.atomic():
                duplicate = self.unchanged_snapshot(observation_key, signature, payload, dependencies)
                if duplicate:
                    return duplicate
        payload["findings"] = self.inspection(collection, doc, enforced) if doc else []
        with self.state.atomic():
            duplicate = (self.unchanged_snapshot(observation_key, signature, payload, dependencies)
                         if token is None else None)
            if duplicate:
                return duplicate
            receipt = self.state.get("capture_receipts", observation_key, {})
            # The prior receipt distinguishes A -> B -> A and delete/reinsert from
            # an unchanged rescan. Scan generations are only for deletion detection.
            event_id = (fingerprint([collection, encoded(token)]) if token is not None else
                        fingerprint([signature, receipt.get("event_id")]))
            if self.state.capture(event_id, collection, payload, encoded(token) if token is not None else None):
                # Keep the latest snapshot boundary even if a stream capture arrives
                # before that snapshot is processed. Older buffered updates cannot
                # replace the state proved by a later unchanged read.
                snapshot_read = receipt.get("snapshot_read")
                if operation == "snapshot" and (not snapshot_read or
                        timestamp(payload["read_started_at"]) >= timestamp(snapshot_read["read_started_at"])):
                    snapshot_read = {"event_id": event_id, "read_started_at": payload["read_started_at"], "observed_at": observed_at}
                self.state.put("capture_receipts", observation_key, {"signature": signature, "event_id": event_id,
                    "read_started_at": payload["read_started_at"], "observed_at": observed_at,
                    "dependencies": dependencies, "snapshot_read": snapshot_read})
        return event_id

    def process_one(self, event):
        name, data = event["collection"], event["data"]
        raw_id = decoded(data["document_key"])
        entity = identifier(raw_id) or fingerprint(data["document_key"])
        observation_key = name + ":" + entity
        with self.state.atomic():
            old = self.state.get("observations", observation_key)
            receipt = self.state.get("capture_receipts", observation_key, {})
            snapshot_read = receipt.get("snapshot_read") or {}
            if data["operation"] == "snapshot" and snapshot_read.get("event_id") == event["id"]:
                data = {**data, "read_started_at": snapshot_read["read_started_at"], "observed_at": snapshot_read["observed_at"]}
            if data.get("missing_document"):
                record_security(self.state, {**event, 'data': data}, entity, self.config, stale=True)
                self.state.put("reconcile", "all", {"requested_at": stamp()})
                self.state.processed(event["id"])
                return True
            prior = old or self.state.get("observation_tombstones", observation_key)
            stale = False
            if prior:
                previous_order, incoming_order = prior.get("source_order"), data.get("source_order")
                if previous_order and incoming_order and tuple(incoming_order) <= tuple(previous_order):
                    stale = True
                if data["operation"] == "snapshot" or data.get("reconciled"):
                    # The query can finish after a newer stream observation was processed.
                    stale |= timestamp(prior["observed_at"]) > timestamp(data.get("read_started_at") or data["observed_at"])
                elif prior.get("operation") == "snapshot" and timestamp(data.get("occurred_at")):
                    # An old buffered event is not the current database state.
                    stale |= timestamp(data["occurred_at"]) < timestamp(prior.get("read_started_at") or prior["observed_at"])
            record_security(self.state, {**event, 'data': data}, entity, self.config, stale=stale)
            if not data["historical"] and data["operation"] != "snapshot":
                known_before = bool(old) and not stale and not data.get("reconciled")
                if old and old.get("operation") == "snapshot":
                    known_before &= bool(timestamp(data.get("occurred_at")) and timestamp(old["observed_at"]) < timestamp(data["occurred_at"]))
                before = old["values"] if known_before else {}
                after = dict(before)
                if data["operation"] == "update":
                    after.update(data.get("transition", {}))
                    for field in data.get("removed", []):
                        after.pop(field, None)
                else:
                    after = data["values"] if data["operation"] != "delete" else {}
                self.state.put("transitions", event["id"], {"collection": name, "entity": entity,
                    "before": before, "after": after, "at": data.get("occurred_at") or data["observed_at"], "historical": False,
                    "operation": data["operation"], "confidence": "observed" if known_before and after == data["values"] else "uncertain"})
            if stale:
                receipt = self.state.get("capture_receipts", observation_key, {})
                if receipt.get("event_id") == event["id"]:
                    # A later, fresh read of this content must not be suppressed.
                    self.state.put("capture_receipts", observation_key, {**receipt, "stale": True})
                self.state.processed(event["id"])
                return True
            if data["operation"] == "delete" or (old or {}).get("document_fingerprint") != data.get("document_fingerprint"):
                # Referencing documents need another check when their targets change,
                # even if the referencing document itself is unchanged.
                self.state.put("source_revisions", name, {"event_id": event["id"]})
            if data["operation"] == "delete":
                self.state.delete("observations", observation_key)
                self.state.put("observation_tombstones", observation_key, {"observed_at": data["observed_at"],
                    "source_order": data.get("source_order"), "operation": "delete"})
                # Recheck all known referencing collections after propagation settles.
                self.state.put("reconcile", "all", {"requested_at": stamp()})
                self.state.delete("reference_checks", observation_key)
                for finding_id, finding in self.state.rows("findings"):
                    if finding.get("collection") == name and finding.get("entity") == digest(entity, self.secret) and finding.get("status") == "open":
                        finding.update(status="resolved", resolved_at=stamp())
                        self.state.put("findings", finding_id, finding)
            else:
                enforced = sorted(set(data.get("required_enforced", [])) | set((old or {}).get("required_enforced", [])))
                self.state.put("observations", observation_key, {"values": data["values"], "first_seen": (old or {}).get("first_seen", event["data"]["observed_at"]),
                    "observed_at": data["observed_at"], "document_key": data["document_key"], "schema_version": data["schema_version"],
                    "operation": data["operation"], "source_order": data.get("source_order"),
                    "document_fingerprint": data.get("document_fingerprint"),
                    "capture_event_id": event["id"],
                    "required_enforced": enforced,
                    "read_started_at": data.get("read_started_at") or data["observed_at"]})
                self.state.delete("observation_tombstones", observation_key)
                self.state.put("reference_checks", observation_key, {"collection": name, "document_key": data["document_key"],
                    "due": (timestamp(data["observed_at"]) + timedelta(seconds=self.config.reference_delay)).isoformat(),
                    "historical": data["historical"], "generation": event["id"], "required_enforced": enforced})
            for finding in data["findings"]:
                self.finding(name, entity, finding, data, event["id"])
            self.state.processed(event["id"])
        return True

    def finding(self, collection, entity, finding, data, source_event):
        # A persistent violation generates once; a correction permits a later recurrence.
        identity = fingerprint([collection, entity, finding["rule"], finding.get("field"), data["schema_version"]])
        prior = self.state.get("findings", identity)
        if prior and prior.get("status") == "open":
            return
        record = {**finding, "id": identity, "event_id": fingerprint([identity, source_event]), "collection": collection,
                  "entity": digest(entity, self.secret), "schema_version": data["schema_version"],
                  "detected_at": stamp(), "operation": data["operation"], "status": "open", "historical": data["historical"],
                  **data.get("provenance", self.registry.provenance())}
        self.state.put("findings", identity, record)
        if not data["historical"]:
            for sink in ("mqtt", "syslog"):
                self.state.put("deliveries", record["event_id"] + ":" + sink,
                    {"sink": sink, "event": record, "status": "pending", "attempts": 0, "available_at": stamp()})

    def check_references(self):
        for key, task in list(self.state.rows("reference_checks"))[:100]:
            if timestamp(task["due"]) > datetime.now(timezone.utc):
                continue
            collection = task["collection"]
            schema = self.registry.contracts().get(collection)
            if not schema:
                continue
            doc = self.source[collection].find_one({"_id": decoded(task["document_key"])}, max_time_ms=3000)
            issues = []
            if doc:
                for ref in schema.get("references", []):
                    target, field = ref.get("collection"), ref.get("field")
                    if target not in self.registry.contracts() or not field:
                        continue
                    value = field_value(doc, field)
                    if value is MISSING:
                        value = None
                    if value is None and ref.get("optional", True):
                        continue
                    values = value if isinstance(value, list) else [value]
                    if len(values) > 1000:
                        issues.append({"rule": "reference_budget_exceeded", "field": field, "severity": "warning"})
                    for v in values[:1000]:
                        if v is None and not ref.get("optional", True) or v is not None and not self.source[target].find_one({ref.get("target_field", ref.get("primary_key", "_id")): v}, {"_id": 1}, max_time_ms=2000):
                            issues.append({"rule": "orphan_reference", "field": field, "severity": "error"})
                            break
                # Expected repeated household expiry must converge after the grace delay.
                if collection == "members" and "groups" in self.registry.contracts() and doc.get("groupName"):
                    group = self.source["groups"].find_one({"groupName": str(doc["groupName"])}, {"expiry": 1}, max_time_ms=2000)
                    if group and timestamp(group.get("expiry")) and timestamp(doc.get("expirationTime")) != timestamp(group["expiry"]):
                        issues.append({"rule": "household_expiry_conflict", "field": "expirationTime", "severity": "warning"})
                issues.extend(self.inspection(collection, doc, task.get("required_enforced", [])))
            data = {"schema_version": self.registry.version, "operation": "reconcile", "historical": task["historical"]}
            entity = key.split(":", 1)[1]
            with self.state.atomic():
                if self.state.get("reference_checks", key) != task:
                    continue  # A newer source event scheduled another check while queries ran.
                for issue in issues:
                    self.finding(collection, entity, issue, data, fingerprint([key, task.get("generation"), stamp()]))
                current_rules = {(v["rule"], v.get("field")) for v in issues}
                for fid, finding in self.state.rows("findings"):
                    if finding["collection"] == collection and finding["entity"] == digest(entity, self.secret) and (finding["rule"], finding.get("field")) not in current_rules:
                        finding.update(status="resolved", resolved_at=stamp())
                        self.state.put("findings", fid, finding)
                self.state.delete("reference_checks", key)

    def start(self):
        for collection in self.registry.contracts():
            started = self.started[collection] = Event()
            thread = Thread(target=self.watch, args=(collection, started), daemon=True)
            self.threads.append(thread)
            thread.start()
        deadline = time.monotonic() + 60
        for started in self.started.values():
            if not started.wait(max(0, deadline - time.monotonic())):
                raise RuntimeError("Source stream startup failed; baseline cannot begin")

    def retired(self, collection):
        if collection in self.registry.contracts():
            return False
        self.state.delete("health", "stream:" + collection)
        return True

    def watch(self, collection, started):
        while not self.stop.is_set() and not self.retired(collection):
            options = {"full_document": "updateLookup", "max_await_time_ms": 1000, "batch_size": self.config.batch_size}
            token = self.state.checkpoint(collection)
            if token:
                options["resume_after"] = decoded(token)
            try:
                self.state.put("health", "stream:" + collection, {"status": "connecting", "caught_up": False, "updated_at": stamp()})
                with self.source[collection].watch(**options) as stream:
                    security_start = stamp()
                    coverage_key = collection + ':' + security_start
                    started.set()
                    while not self.stop.is_set() and not self.retired(collection):
                        event = stream.try_next()
                        if event:
                            operation = event["operationType"]
                            if operation in {"invalidate", "drop", "rename", "dropDatabase"}:
                                raise OperationFailure("Stream invalidated", code=286)
                            updates = event.get("updateDescription", {}).get("updatedFields", {})
                            transition = normalize(collection, updates, self.secret)
                            if collection == "invoices" and not {"amount", "transaction_id"} <= updates.keys():
                                # A partial update cannot imply that a paid invoice became unpaid.
                                transition.pop("paid_positive", None)
                            removed = [f for f in event.get("updateDescription", {}).get("removedFields", []) if f in ALLOWED.get(collection, set())]
                            # Normalized paths need the same aliases for removals as additions.
                            removed = [{"uid": "uid_hash", "last_failed_transaction_id": "payment_failed"}.get(f, f) for f in removed]
                            self.capture(collection, event.get("fullDocument"), operation, event["_id"],
                                         document_id=event.get("documentKey", {}).get("_id"), transition=transition, removed=removed,
                                         changed_fields=list(updates) + event.get("updateDescription", {}).get("removedFields", []),
                                         updated_fields=updates,
                                         security_removed=event.get("updateDescription", {}).get("removedFields", []),
                                         occurred_at=event["clusterTime"].as_datetime().isoformat() if event.get("clusterTime") else None,
                                         source_order=[event["clusterTime"].time, event["clusterTime"].inc] if event.get("clusterTime") else None)
                        elif stream.resume_token:
                            # Empty batch post-batch tokens advance only after all preceding captures committed.
                            self.state.advance_checkpoint(collection, encoded(stream.resume_token))
                        if self.retired(collection):
                            return
                        self.state.put("health", "stream:" + collection, {"status": "ready", "caught_up": event is None, "updated_at": stamp()})
                        if event is None and security_enabled(self.config) and collection in SECURITY_COLLECTIONS:
                            self.state.put('security_coverage', coverage_key, {'collection': collection,
                                'start': security_start, 'end': stamp()})
            except OperationFailure as exc:
                if self.retired(collection):
                    return
                if exc.code in {136, 237, 260, 280, 286}:
                    self.gap(collection)
                else:
                    self.state.put("health", "stream:" + collection, {"status": "error", "caught_up": False, "error": type(exc).__name__, "updated_at": stamp()})
                self.stop.wait(5)
            except Exception as exc:
                if self.retired(collection):
                    return
                log.warning("Anomaly stream retry collection=%s error=%s", collection, type(exc).__name__)
                self.state.put("health", "stream:" + collection, {"status": "retry", "caught_up": False, "updated_at": stamp()})
                self.stop.wait(5)

    def gap(self, collection):
        # Clear stale resume token using a committed gap marker, never silently start over.
        with self.state.atomic():
            previous = self.state.get("health", "stream:" + collection, {})
            start = previous.get("updated_at", stamp())
            self.state.put("gaps", fingerprint([collection, start]), {"collection": collection, "start": start, "detected_at": stamp(), "end": None})
            self.state.put("health", "stream:" + collection, {"status": "gap", "caught_up": False, "updated_at": stamp()})
            self.state.put("health", "coverage_gap", {"status": "gap", "unresolved": True, "updated_at": stamp()})
            self.state.advance_checkpoint(collection, {})
            self.state.put("reconcile", "all", {"requested_at": stamp()})

    def scan(self, historical=False):
        generation = stamp()
        for collection in self.registry.contracts():
            last = None
            while not self.stop.is_set():
                query = {"_id": {"$gt": last}} if last is not None else {}
                read_started_at = stamp()
                docs = list(self.source[collection].find(query).sort("_id", 1).limit(self.config.batch_size).max_time_ms(10000))
                if not docs:
                    break
                for doc in docs:
                    self.capture(collection, doc, "snapshot", historical=historical, generation=generation, read_started_at=read_started_at)
                    # Reconciliation rereads current state; it never invents historical transitions.
                    observation_key = collection + ":" + (identifier(doc["_id"]) or fingerprint(encoded(doc["_id"])))
                    self.state.put("scan_seen", observation_key, {"generation": generation})
                last = docs[-1]["_id"]
                self.stop.wait(.01)
            if self.stop.is_set():
                return  # An interrupted scan is not evidence of deletion or completed coverage.
            # Recheck missing records before removing them to tolerate concurrent insert/reassignment.
            for key, row in self.state.rows("observations", prefix=collection + ":"):
                if key.startswith(collection + ":") and self.state.get("scan_seen", key, {}).get("generation") != generation:
                    doc_id = decoded(row["document_key"])
                    read_started_at = stamp()
                    if not self.source[collection].find_one({"_id": doc_id}, {"_id": 1}, max_time_ms=2000):
                        self.capture(collection, None, "delete", document_id=doc_id, historical=historical,
                                     generation=generation, read_started_at=read_started_at, reconciled=True)
        self.state.put("health", "scan", {"status": "ready", "completed_at": stamp(), "updated_at": stamp()})
        with self.state.atomic():
            requested = timestamp(self.state.get("reconcile", "all", {}).get("requested_at"))
            if requested is None or requested <= timestamp(generation):
                self.state.delete("reconcile", "all")
            completed_at = stamp()
            for key, gap in list(self.state.rows("gaps")):
                stream = self.state.get("health", "stream:" + gap["collection"], {})
                updated = timestamp(stream.get("updated_at"))
                if (gap.get("end") is None and stream.get("status") == "ready" and stream.get("caught_up") is True
                        and updated and timestamp(completed_at) - updated <= timedelta(seconds=30)
                        and timestamp(gap.get("detected_at") or gap["start"]) <= timestamp(generation)):
                    gap["end"] = completed_at
                    self.state.put("gaps", key, gap)
            if self.state.get("health", "coverage_gap"):
                unresolved = any(gap.get("end") is None for _, gap in self.state.rows("gaps"))
                self.state.put("health", "coverage_gap", {"status": "gap" if unresolved else "ready", "unresolved": unresolved, "updated_at": completed_at})

    def drain(self, limit=100):
        events = self.state.pending(limit)
        for event in events:
            if event["data"].get("checkpoint_only"):
                self.state.processed(event["id"])
            else:
                self.process_one(event)
        return len(events)
