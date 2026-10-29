"""Read-only ingestion, crash recovery, schema drift, and concurrent snapshot fixtures."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from bson import Decimal128, Int64, ObjectId, Timestamp
import pytest

from ledger.anomaly.ingest import Ingestor, MISSING, encoded, field_value, verify_source
from ledger.anomaly.__main__ import preflight_manifest
from ledger.anomaly.schema import SchemaRegistry, accepts, compatibility, validate_document
from ledger.anomaly.state import MemoryState


def matches(document, query, insensitive=False):
    """Small query interpreter for the read operations exercised by these fixtures."""
    def equal(left, right):
        if isinstance(left, list):
            return any(equal(item, right) for item in left)
        if insensitive and isinstance(left, str) and isinstance(right, str):
            return left.casefold() == right.casefold()
        return (None if left is MISSING else left) == right
    for key, expected in query.items():
        if key == "$and":
            if not all(matches(document, clause, insensitive) for clause in expected): return False
        elif key == "$or":
            if not any(matches(document, clause, insensitive) for clause in expected): return False
        else:
            value = field_value(document, key)
            if isinstance(expected, dict):
                for operator, operand in expected.items():
                    if operator == "$ne" and equal(value, operand): return False
                    if operator == "$gt" and (value is MISSING or value <= operand): return False
                    if operator == "$exists" and (value is not MISSING) != operand: return False
                    if operator == "$in" and not any(equal(value, item) for item in operand): return False
                    if operator == "$type" and not (operand == "string" and isinstance(value, str)): return False
                    assert operator in {"$ne", "$gt", "$exists", "$in", "$type"}, operator
            elif not equal(value, expected):
                return False
    return True


class Cursor:
    def __init__(self, rows): self.rows = deepcopy(rows)
    def sort(self, field, direction):
        self.rows.sort(key=lambda row: row[field], reverse=direction == -1)
        return self
    def limit(self, count):
        self.rows = self.rows[:count]
        return self
    def max_time_ms(self, value): return self
    def __iter__(self): return iter(self.rows)


class Stream:
    def __init__(self, collection):
        self.collection = collection
        self.events = iter(collection.events)
        self.resume_token = None
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def try_next(self):
        event = next(self.events, None)
        if event:
            self.resume_token = event["_id"]
        elif self.collection.on_end:
            self.collection.on_end()
        return event


class Collection:
    """Intentionally has no insert/update/delete API: source mutations fail tests."""
    def __init__(self, rows=()):
        self.rows, self.calls, self.events = deepcopy(list(rows)), [], []
        self.on_end = None
    def find_one(self, query, projection=None, **options):
        self.calls.append((deepcopy(query), projection, options))
        row = next((row for row in self.rows if matches(row, query, bool(options.get("collation")))), None)
        if row is None: return None
        return deepcopy({key: row[key] for key, wanted in projection.items() if wanted and key in row} if projection else row)
    def find(self, query):
        return Cursor([row for row in self.rows if matches(row, query)])
    def watch(self, **options):
        self.calls.append(("watch", options))
        return Stream(self)


class Source(dict):
    def command(self, command):
        assert command == "ping"
        return {"ok": 1}


def contract(**extra):
    return {"fields": {"_id": {"types": ["Object"]}, "status": {"types": ["String"]}},
            "required": ["_id"], "dynamic": True, "indexes": [], "references": [], **extra}


def setup(collections=None, rows=None, state=None):
    collections = collections or {"members": contract()}
    state = state or MemoryState()
    source = Source({name: Collection((rows or {}).get(name, [])) for name in collections})
    registry = SchemaRegistry(state, {"format_version": 1, "collections": collections})
    registry.install()
    ingestor = Ingestor(state, source, registry, SimpleNamespace(reference_delay=0, batch_size=2))
    return ingestor, state, source


def at(monkeypatch, instant):
    monkeypatch.setattr("ledger.anomaly.ingest.stamp", lambda: instant)


def process(ingestor, state, event_id):
    ingestor.process_one(next(row for row in state.pending() if row["id"] == event_id))


def test_bson_date_integer_decimal_and_embedded_stored_paths():
    assert accepts(datetime.now(timezone.utc), ["Date"])
    assert accepts(Int64(2**40), ["Integer"])
    assert accepts(Decimal128("1.25"), ["BSON::Decimal128"])
    assert accepts(ObjectId(), ["FixTicketId"])
    assert not accepts(True, ["Integer"])
    schema = contract(fields={"items": {"types": ["Array"], "embedded": {
        "fields": {"stored": {"types": ["Integer"]}}, "required": ["stored"], "dynamic": False}}}, required=[])
    findings = validate_document(schema, {"items": [{"stored": "secret", "private-secret": True}, {"stored": "secret"}, {}]})
    assert {finding["rule"] for finding in findings} == {"type", "unknown_field", "required"}
    assert len([finding for finding in findings if finding["rule"] == "type"]) == 1
    assert {finding["field"] for finding in findings if finding["rule"] != "unknown_field"} == {"items[].stored"}
    assert "secret" not in json.dumps(findings)


def test_new_embedded_requirement_and_unique_constraint_require_schema_review():
    old = contract(fields={"item": {"types": ["Hash"], "embedded": {"fields": {"code": {"types": ["String"]}}, "required": []}}})
    new = deepcopy(old)
    new["fields"]["item"]["embedded"]["required"] = ["code"]
    new["indexes"] = [{"keys": {"item.code": 1}, "unique": True}]
    assert compatibility({"items": old}, {"items": new}) == {"items": ["embedded_changed:item", "unique_index_changed"]}


def test_schema_review_keeps_independent_optional_additions_including_embedded_fields():
    old = contract(fields={"code": {"types": ["String"]}, "item": {"types": ["Hash"], "embedded": {
        "fields": {"status": {"types": ["String"], "enum": ["active", "expired"]}}, "required": []}}}, required=[])
    ingestor, state, _ = setup({"items": old})
    candidate = deepcopy(ingestor.registry.manifest)
    new = candidate["collections"]["items"]
    new["fields"]["code"]["types"] = ["Integer"]
    new["fields"]["optional"] = {"types": ["String"]}
    embedded = new["fields"]["item"]["embedded"]
    embedded["fields"]["status"]["enum"] = ["active"]
    embedded["fields"]["extra"] = {"types": ["Integer"]}
    embedded["required"] = ["extra"]
    registry = SchemaRegistry(state, candidate)
    assert registry.install()["pending"]["items"]
    effective = registry.contracts()["items"]
    assert effective["fields"]["code"]["types"] == ["String"]
    assert "optional" in effective["fields"]
    assert effective["fields"]["item"]["embedded"]["fields"]["status"]["enum"] == ["active", "expired"]
    assert "extra" in effective["fields"]["item"]["embedded"]["fields"]
    assert effective["fields"]["item"]["embedded"]["required"] == []
    assert state.get("health", "schema")["status"] == "review"
    registry.approve(registry.version, "admin")
    assert registry.contracts()["items"] == new
    assert state.get("health", "schema")["status"] == "ready"


def test_build_clock_does_not_change_contract_revision_and_findings_have_safe_provenance():
    ingestor, state, _ = setup()
    manifest = deepcopy(ingestor.registry.manifest)
    manifest["provenance"] = {"rails_commit": "a" * 40, "contract_hash": "b" * 64, "extractor_version": "1",
                              "build_time": "2026-01-01T00:00:00Z", "repository": "private-repository-url"}
    first = SchemaRegistry(state, manifest)
    later = deepcopy(manifest)
    later["provenance"]["build_time"] = "2026-01-02T00:00:00Z"
    assert first.version == SchemaRegistry(state, later).version
    ingestor.registry = first
    first.install()
    ingestor.capture("members", {"_id": "m1", "status": 9}, "insert")
    ingestor.drain()
    finding = next(state.rows("findings"))[1]
    assert finding["rails_commit"] == "a" * 40 and finding["contract_hash"] == "b" * 64
    assert finding["extractor_version"] == "1" and finding["build_time"] == "2026-01-01T00:00:00Z"
    assert "private-repository-url" not in json.dumps(finding)


def test_new_required_field_does_not_retroactively_invalidate_legacy_records(monkeypatch):
    legacy = {"_id": "m1", "status": "activeMember"}
    ingestor, state, source = setup(rows={"members": [legacy]})
    at(monkeypatch, "2026-01-01T00:00:01+00:00")
    ingestor.capture("members", legacy, "snapshot", historical=True)
    ingestor.drain()
    candidate = deepcopy(ingestor.registry.manifest)
    candidate["collections"]["members"]["fields"]["new_code"] = {"types": ["String"]}
    candidate["collections"]["members"]["required"].append("new_code")
    ingestor.registry = SchemaRegistry(state, candidate)
    ingestor.registry.install()
    monkeypatch.setattr("ledger.anomaly.schema.stamp", lambda: "2026-01-01T00:00:02+00:00")
    ingestor.registry.approve(ingestor.registry.version, "admin")
    at(monkeypatch, "2026-01-01T00:00:03+00:00")
    ingestor.capture("members", legacy, "snapshot", generation="after-approval")
    ingestor.drain()
    ingestor.check_references()
    assert list(state.rows("findings")) == []
    ingestor.capture("members", legacy, "update", {"token": 1}, changed_fields=["status"], occurred_at="2026-01-01T00:00:03+00:00")
    ingestor.drain()
    ingestor.check_references()
    assert list(state.rows("findings")) == []
    # Explicitly removing the new field after approval is a new violation.
    ingestor.capture("members", legacy, "update", {"token": 2}, changed_fields=["new_code"], occurred_at="2026-01-01T00:00:04+00:00")
    ingestor.drain()
    assert [row["field"] for _, row in state.rows("findings")] == ["new_code"]
    ingestor.capture("members", legacy, "snapshot", generation="next-scan")
    ingestor.drain()
    ingestor.check_references()
    assert next(state.rows("findings"))[1]["status"] == "open"
    assert state.get("observations", "members:m1")["required_enforced"] == ["new_code"]
    # A newly inserted member is checked; a buffered pre-approval insert is legacy.
    ingestor.capture("members", {"_id": "new"}, "insert", {"token": 3}, occurred_at="2026-01-01T00:00:04+00:00")
    ingestor.capture("members", {"_id": "buffered"}, "insert", {"token": 4}, occurred_at="2026-01-01T00:00:01+00:00")
    ingestor.drain()
    assert len(list(state.rows("findings"))) == 2
    assert state.get("observations", "members:new")["required_enforced"] == ["new_code"]
    assert state.get("observations", "members:buffered")["required_enforced"] == []


def test_capture_and_processing_replay_keep_one_event_and_private_values_out_of_state():
    ingestor, state, _ = setup()
    doc = {"_id": "m1", "status": "activeMember", "email": "private@example.invalid", "password": "never-store"}
    event_id = ingestor.capture("members", doc, "insert", {"token": 1})
    ingestor.drain()
    restarted, _, _ = setup(state=state)
    assert restarted.secret == ingestor.secret
    assert restarted.capture("members", doc, "insert", {"token": 1}) == event_id
    assert state.pending() == []
    assert state.checkpoint("members") == {"token": 1}
    stored = json.dumps(list(state.documents.values()) + list(state.events.values()))
    assert "never-store" not in stored and "private@example.invalid" not in stored


def test_processing_crash_rolls_back_observation_findings_deliveries_and_processed_flag(monkeypatch):
    ingestor, state, _ = setup({"members": contract(fields={"status": {"types": ["String"]}}, required=["status"])})
    event_id = ingestor.capture("members", {"_id": "m1", "status": 99}, "insert", {"token": 1})
    original = state.processed
    def fail_after_processed(key):
        original(key)
        raise RuntimeError("simulated process crash")
    monkeypatch.setattr(state, "processed", fail_after_processed)
    with pytest.raises(RuntimeError): ingestor.drain()
    assert state.get("observations", "members:m1") is None
    assert list(state.rows("findings")) == [] and list(state.rows("deliveries")) == []
    assert [event["id"] for event in state.pending()] == [event_id]
    assert state.checkpoint("members") == {"token": 1}  # Capture was already durable.
    monkeypatch.setattr(state, "processed", original)
    ingestor.drain()
    assert len(list(state.rows("findings"))) == 1 and len(list(state.rows("deliveries"))) == 2


def test_capture_failure_rolls_back_event_and_checkpoint():
    ingestor, state, _ = setup()
    with pytest.raises(RuntimeError):
        with state.atomic():
            ingestor.capture("members", {"_id": "m1"}, "insert", {"token": 1})
            raise RuntimeError("simulated commit failure")
    assert state.pending() == [] and state.checkpoint("members") is None
    assert list(state.rows("capture_receipts")) == []


def test_idle_checkpoint_is_transactional_without_retained_events():
    state = MemoryState()
    state.advance_checkpoint("members", {"token": 1})
    with pytest.raises(RuntimeError):
        with state.atomic():
            state.advance_checkpoint("members", {"token": 2})
            raise RuntimeError("simulated commit failure")
    assert state.checkpoint("members") == {"token": 1}
    assert not state.events
    state.advance_checkpoint("members", {"token": 3})
    assert state.checkpoint("members") == {"token": 3}
    assert not state.events


def test_missing_update_lookup_preserves_state_and_requests_reconciliation():
    ingestor, state, _ = setup()
    ingestor.capture("members", {"_id": "m1", "status": "activeMember"}, "snapshot")
    ingestor.drain()
    prior = state.get("observations", "members:m1")
    ingestor.capture("members", None, "update", {"token": 2}, document_id="m1")
    ingestor.drain()
    assert state.get("observations", "members:m1") == prior
    assert state.get("reconcile", "all") and state.pending() == []
    assert state.checkpoint("members") == {"token": 2}
    assert list(state.rows("transitions")) == []


@pytest.mark.parametrize("sparse,documents,duplicate", [
    (False, [{"_id": "m1"}, {"_id": "m2"}], True),
    (True, [{"_id": "m1"}, {"_id": "m2"}], False),
    (True, [{"_id": "m1", "code": None}, {"_id": "m2", "code": None}], True),
    (True, [{"_id": "m1", "code": None}, {"_id": "m2"}], False),
])
def test_unique_index_missing_and_null_respect_sparse_semantics(sparse, documents, duplicate):
    ingestor, _, _ = setup({"members": contract(indexes=[{"keys": {"code": 1}, "unique": True, "sparse": sparse}])}, {"members": documents})
    assert any(f["rule"] == "duplicate" for f in ingestor.inspection("members", documents[0])) is duplicate


def test_compound_sparse_nested_and_partial_collated_indexes():
    documents = [{"_id": "m1", "profile": {"code": "ABC"}, "active": "YES"}, {"_id": "m2", "profile": {"code": "abc"}, "active": "yes"}]
    index = {"keys": {"profile.code": 1, "other": 1}, "unique": True, "sparse": True,
             "partial_filter_expression": {"active": "yes"}, "collation": {"locale": "en", "strength": 2}}
    ingestor, _, source = setup({"members": contract(indexes=[index])}, {"members": documents})
    assert any(f["rule"] == "duplicate" for f in ingestor.inspection("members", documents[0]))
    assert all(call[2].get("collation") == index["collation"] for call in source["members"].calls)


def test_references_use_declared_target_field_and_corrected_duplicate_can_recur():
    members = contract(indexes=[{"keys": {"code": 1}, "unique": True}],
        references=[{"field": "household.name", "collection": "groups", "target_field": "name", "optional": False}])
    docs = [{"_id": "m1", "code": "a", "household": {"name": "family"}}, {"_id": "m2", "code": "a"}]
    ingestor, state, source = setup({"members": members, "groups": contract()}, {"members": docs, "groups": [{"_id": "g1", "name": "family"}]})
    ingestor.capture("members", docs[0], "insert", {"token": 1})
    ingestor.drain()
    ingestor.check_references()
    findings = list(state.rows("findings"))
    assert [row["rule"] for _, row in findings] == ["duplicate"]
    assert any(call[0] == {"name": "family"} for call in source["groups"].calls)
    source["members"].rows[1]["code"] = "b"
    ingestor.capture("members", docs[0], "snapshot", generation="correction")
    ingestor.drain()
    ingestor.check_references()
    assert state.get("findings", findings[0][0])["status"] == "resolved"
    source["members"].rows[1]["code"] = "a"
    ingestor.capture("members", docs[0], "update", {"token": 2})
    ingestor.drain()
    assert state.get("findings", findings[0][0])["status"] == "open"
    assert len(list(state.rows("deliveries"))) == 4


def test_delayed_snapshot_cannot_overwrite_stream_or_resurrect_delete(monkeypatch):
    ingestor, state, _ = setup()
    at(monkeypatch, "2026-01-01T00:00:02+00:00")
    stream = ingestor.capture("members", {"_id": "m1", "status": "revoked"}, "update", {"token": 2}, source_order=[2, 1])
    at(monkeypatch, "2026-01-01T00:00:03+00:00")
    snapshot = ingestor.capture("members", {"_id": "m1", "status": "activeMember"}, "snapshot", read_started_at="2026-01-01T00:00:01+00:00")
    process(ingestor, state, stream)
    process(ingestor, state, snapshot)
    assert state.get("observations", "members:m1")["values"]["status"] == "revoked"
    at(monkeypatch, "2026-01-01T00:00:04+00:00")
    ingestor.capture("members", None, "delete", {"token": 3}, document_id="m1", source_order=[3, 1])
    ingestor.drain()
    at(monkeypatch, "2026-01-01T00:00:05+00:00")
    ingestor.capture("members", {"_id": "m1", "status": "activeMember"}, "snapshot", generation="later", read_started_at="2026-01-01T00:00:03+00:00")
    ingestor.drain()
    assert state.get("observations", "members:m1") is None


def test_buffered_event_before_snapshot_cannot_become_a_known_historical_before(monkeypatch):
    ingestor, state, _ = setup()
    at(monkeypatch, "2026-01-01T00:00:10+00:00")
    ingestor.capture("members", {"_id": "m1", "status": "revoked"}, "snapshot")
    ingestor.drain()
    at(monkeypatch, "2026-01-01T00:00:11+00:00")
    event = ingestor.capture("members", {"_id": "m1", "status": "activeMember"}, "insert", {"token": 1}, occurred_at="2026-01-01T00:00:01+00:00")
    ingestor.drain()
    assert state.get("observations", "members:m1")["values"]["status"] == "revoked"
    transition = state.get("transitions", event)
    assert transition["confidence"] == "uncertain" and transition["before"] == {}


def test_snapshot_strictly_before_live_change_is_observed_only_when_delta_matches(monkeypatch):
    ingestor, state, _ = setup()
    at(monkeypatch, "2026-01-01T00:00:01+00:00")
    ingestor.capture("members", {"_id": "m1", "status": "activeMember"}, "snapshot")
    ingestor.drain()
    at(monkeypatch, "2026-01-01T00:00:03+00:00")
    event = ingestor.capture("members", {"_id": "m1", "status": "revoked"}, "update", {"token": 2},
        transition={"status": "revoked"}, occurred_at="2026-01-01T00:00:02+00:00")
    ingestor.drain()
    assert state.get("transitions", event)["confidence"] == "observed"
    later = ingestor.capture("members", {"_id": "m1", "status": "pending"}, "update", {"token": 3},
        transition={"status": "inactive"}, occurred_at="2026-01-01T00:00:03+00:00")
    ingestor.drain()
    assert state.get("transitions", later)["confidence"] == "uncertain"


def test_timestamp_increment_orders_events_and_inferred_delete_is_uncertain(monkeypatch):
    ingestor, state, source = setup()
    at(monkeypatch, "2026-01-01T00:00:02+00:00")
    later = ingestor.capture("members", {"_id": "m1", "status": "revoked"}, "insert", {"token": 2}, source_order=[100, 2])
    earlier = ingestor.capture("members", {"_id": "m1", "status": "activeMember"}, "insert", {"token": 1}, source_order=[100, 1])
    process(ingestor, state, later)
    process(ingestor, state, earlier)
    assert state.get("observations", "members:m1")["values"]["status"] == "revoked"
    at(monkeypatch, "2026-01-01T00:00:04+00:00")
    ingestor.scan()
    ingestor.drain()
    deletion = next(row for _, row in state.rows("transitions") if row["operation"] == "delete")
    assert deletion["confidence"] == "uncertain" and deletion["before"] == {}
    assert state.get("observations", "members:m1") is None


def test_scan_deduplicates_unchanged_documents_and_interruption_is_not_complete(monkeypatch):
    ingestor, state, _ = setup(rows={"members": [{"_id": "m1", "status": "activeMember"}]})
    at(monkeypatch, "2026-01-01T00:00:01+00:00")
    ingestor.scan(historical=True)
    ingestor.drain()
    ingestor.check_references()
    assert state.get("reference_checks", "members:m1") is None
    def unexpected_inspection(*args):
        pytest.fail("An unchanged snapshot must not repeat validation")
    monkeypatch.setattr(ingestor, "inspection", unexpected_inspection)
    at(monkeypatch, "2026-01-01T00:00:02+00:00")
    ingestor.scan()
    ingestor.drain()
    assert len(state.events) == 1
    assert state.get("reference_checks", "members:m1") is None
    assert state.get("scan_seen", "members:m1")["generation"] == "2026-01-01T00:00:02+00:00"
    assert list(state.rows("transitions")) == []
    state.delete("health", "scan")
    ingestor.stop.set()
    ingestor.scan()
    assert state.get("health", "scan") is None
    assert state.get("observations", "members:m1") is not None


def test_snapshot_deduplication_survives_pending_work_restart_and_event_retention(monkeypatch):
    document = {"_id": "m1", "status": "activeMember"}
    ingestor, state, _ = setup()
    first = ingestor.capture("members", document, "snapshot", generation="one")
    assert ingestor.capture("members", document, "snapshot", generation="two") == first
    assert len(state.pending()) == 1
    ingestor.drain()
    state.events.clear()  # Processed-event retention must not reset the snapshot receipt.
    restarted, _, _ = setup(state=state)
    assert restarted.capture("members", document, "snapshot", generation="three") == first
    assert state.pending() == []


def test_snapshots_capture_returns_to_prior_content_and_reinsertions():
    ingestor, state, _ = setup()
    active = {"_id": "m1", "status": "activeMember"}
    first = ingestor.capture("members", active, "snapshot", generation="one")
    ingestor.drain()
    second = ingestor.capture("members", {**active, "status": "inactive"}, "snapshot", generation="two")
    ingestor.drain()
    third = ingestor.capture("members", active, "snapshot", generation="three")
    ingestor.drain()
    assert len({first, second, third}) == 3
    assert state.get("observations", "members:m1")["values"]["status"] == "activeMember"
    ingestor.capture("members", None, "delete", {"token": 1}, document_id="m1")
    ingestor.drain()
    assert ingestor.capture("members", active, "snapshot", generation="four") != third
    ingestor.drain()
    assert state.get("observations", "members:m1")


def test_fresh_snapshot_can_retry_content_previously_discarded_as_stale(monkeypatch):
    ingestor, state, _ = setup()
    at(monkeypatch, "2026-01-01T00:00:03+00:00")
    newer = ingestor.capture("members", {"_id": "m1", "status": "inactive"}, "update", {"token": 1})
    old = {"_id": "m1", "status": "activeMember"}
    stale = ingestor.capture("members", old, "snapshot", read_started_at="2026-01-01T00:00:01+00:00")
    process(ingestor, state, newer)
    process(ingestor, state, stale)
    at(monkeypatch, "2026-01-01T00:00:04+00:00")
    fresh = ingestor.capture("members", old, "snapshot")
    assert fresh != stale
    ingestor.drain()
    assert state.get("observations", "members:m1")["values"]["status"] == "activeMember"


@pytest.mark.parametrize("pending", [False, True])
def test_unchanged_snapshot_advances_stale_event_boundary(monkeypatch, pending):
    ingestor, state, _ = setup()
    document = {"_id": "m1", "status": "activeMember"}
    at(monkeypatch, "2026-01-01T00:00:01+00:00")
    first = ingestor.capture("members", document, "snapshot")
    if not pending:
        ingestor.drain()
    at(monkeypatch, "2026-01-01T00:00:03+00:00")
    assert ingestor.capture("members", document, "snapshot") == first
    assert len(state.events) == 1
    at(monkeypatch, "2026-01-01T00:00:04+00:00")
    ingestor.capture("members", {**document, "status": "revoked"}, "update", {"token": 1},
                     occurred_at="2026-01-01T00:00:02+00:00", transition={"status": "revoked"})
    ingestor.drain()
    assert state.get("observations", "members:m1")["first_seen"] == "2026-01-01T00:00:01+00:00"
    assert state.get("observations", "members:m1")["values"]["status"] == "activeMember"


def test_snapshot_detects_unprojected_structure_changes_without_storing_raw_values():
    schema = contract(fields={"_id": {"types": ["String"]}, "private_field": {"types": ["String"]}})
    ingestor, state, _ = setup({"members": schema})
    first = ingestor.capture("members", {"_id": "m1", "private_field": "never-store"}, "snapshot")
    ingestor.drain()
    second = ingestor.capture("members", {"_id": "m1", "private_field": 123}, "snapshot")
    ingestor.drain()
    assert first != second
    assert state.events[first]["data"]["values"] == state.events[second]["data"]["values"]
    assert any(row["rule"] == "type" for _, row in state.rows("findings"))
    assert "never-store" not in json.dumps(list(state.documents.values()) + list(state.events.values()))


def test_approved_contract_rechecks_unchanged_snapshot_with_same_manifest_version():
    schema = contract(fields={"status": {"types": ["String"], "enum": ["activeMember", "inactive"]}})
    ingestor, state, _ = setup({"members": schema})
    document = {"_id": "m1", "status": "inactive"}
    candidate = deepcopy(ingestor.registry.manifest)
    candidate["collections"]["members"]["fields"]["status"]["enum"] = ["activeMember"]
    ingestor.registry = SchemaRegistry(state, candidate)
    assert ingestor.registry.install()["pending"]
    first = ingestor.capture("members", document, "snapshot")
    ingestor.drain()
    assert not list(state.rows("findings"))
    ingestor.registry.approve(ingestor.registry.version, "admin")
    second = ingestor.capture("members", document, "snapshot")
    ingestor.drain()
    assert first != second
    assert any(row["rule"] == "enum" for _, row in state.rows("findings"))


def test_deduplicated_snapshot_rechecks_deleted_and_restored_reference_targets():
    member = {"_id": "m1", "group_id": "g1"}
    group = {"_id": "g1"}
    schema = contract(references=[{"field": "group_id", "collection": "groups", "optional": False}])
    ingestor, state, source = setup({"members": schema, "groups": contract()},
                                    {"members": [member], "groups": [group]})
    for _ in range(2):  # Establish both the documents and their dependency revisions.
        ingestor.scan()
        ingestor.drain()
        ingestor.check_references()
    assert not list(state.rows("findings"))
    count = len(state.events)
    ingestor.scan()
    ingestor.drain()
    assert len(state.events) == count
    source["groups"].rows.clear()
    ingestor.capture("groups", None, "delete", {"token": 1}, document_id="g1")
    ingestor.drain()
    ingestor.scan()
    ingestor.drain()
    ingestor.check_references()
    assert len(state.events) == count + 1  # Only the target deletion adds an event.
    assert any(row["rule"] == "orphan_reference" and row["status"] == "open" for _, row in state.rows("findings"))
    count = len(state.events)
    ingestor.scan()
    ingestor.drain()
    assert len(state.events) == count
    source["groups"].rows.append(group)
    ingestor.capture("groups", group, "insert", {"token": 2})
    ingestor.drain()
    ingestor.scan()
    ingestor.drain()
    ingestor.check_references()
    assert all(row["status"] == "resolved" for _, row in state.rows("findings"))


def test_idle_post_batch_tokens_only_update_checkpoint(monkeypatch):
    ingestor, state, source = setup()
    class IdleStream:
        resume_token = None
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def try_next(self):
            self.resume_token = {"token": 1 + (self.resume_token or {}).get("token", 0)}
            if self.resume_token["token"] == 100:
                ingestor.stop.set()
            return None
    monkeypatch.setattr(source["members"], "watch", lambda **kwargs: IdleStream())
    ingestor.watch("members", SimpleNamespace(set=lambda: None))
    assert state.checkpoint("members") == {"token": 100}
    assert not state.events
    assert state.get("health", "stream:members")["caught_up"] is True


def test_stream_uses_resume_token_and_normalized_removed_fields_without_source_writes():
    ingestor, state, source = setup({"cards": contract()})
    state.capture("old", "cards", {"checkpoint_only": True}, {"token": 1})
    source["cards"].events = [{"_id": {"token": 2}, "operationType": "update", "documentKey": {"_id": "c1"},
        "fullDocument": {"_id": "c1", "member_id": "m1"}, "clusterTime": Timestamp(100, 2),
        "updateDescription": {"updatedFields": {"member_id": "m1"}, "removedFields": ["uid", "password"]}}]
    source["cards"].on_end = ingestor.stop.set
    ingestor.watch("cards", SimpleNamespace(set=lambda: None))
    watch = next(call for call in source["cards"].calls if call[0] == "watch")
    assert watch[1]["resume_after"] == {"token": 1}
    assert watch[1]["full_document"] == "updateLookup"
    event = next(row for row in state.pending() if not row["data"].get("checkpoint_only"))
    assert event["data"]["removed"] == ["uid_hash"]
    assert event["data"]["source_order"] == [100, 2]
    assert state.checkpoint("cards") == {"token": 2}
    assert state.get("health", "stream:cards")["caught_up"] is True


def test_stream_backlog_is_not_reported_caught_up_until_empty_batch():
    ingestor, state, source = setup()
    source["members"].events = [{"_id": {"token": 1}, "operationType": "insert",
        "documentKey": {"_id": "m1"}, "fullDocument": {"_id": "m1"}, "clusterTime": Timestamp(100, 1)}]
    def after_last_event():
        assert state.get("health", "stream:members")["status"] == "ready"
        assert state.get("health", "stream:members")["caught_up"] is False
        ingestor.stop.set()
    source["members"].on_end = after_last_event
    ingestor.watch("members", SimpleNamespace(set=lambda: None))
    assert state.get("health", "stream:members")["caught_up"] is True


def test_approved_collection_removal_retires_running_watch_and_health():
    ingestor, state, source = setup({"members": contract(), "retired": contract()})
    candidate = {"format_version": 1, "collections": {"members": contract()}}
    ingestor.registry = SchemaRegistry(state, candidate)
    assert "retired" in ingestor.registry.install()["pending"]
    source["retired"].on_end = lambda: ingestor.registry.approve(ingestor.registry.version, "admin")
    ingestor.watch("retired", SimpleNamespace(set=lambda: None))
    assert state.get("health", "stream:retired") is None
    assert not ingestor.stop.is_set()  # Other collection workers continue.
    assert ingestor.capture("retired", {"_id": "x"}, "insert", {"token": 1}) is None


def test_full_preflight_includes_retained_collections_without_mutating_schemas():
    ingestor, state, _ = setup({"members": contract(), "retired": contract()})
    candidate = {"format_version": 1, "collections": {"members": contract(), "new_collection": contract()}}
    before = deepcopy(state.documents)
    required = preflight_manifest(state, candidate)
    assert set(required["collections"]) == {"members", "retired", "new_collection"}
    assert state.documents == before
    assert set(candidate["collections"]) == {"members", "new_collection"}
    source = Source({name: Collection() for name in required["collections"]})
    def denied(**options):
        raise PermissionError("provider response must not escape")
    source["retired"].watch = denied
    with pytest.raises(PermissionError, match="Read/changeStream preflight failed for: retired") as error:
        verify_source(source, required)
    assert "provider response" not in str(error.value)


def test_preflight_accepts_fresh_postgres_but_does_not_hide_other_database_errors():
    class MissingTable(Exception):
        sqlstate = "42P01"
    def absent(*args):
        raise MissingTable()
    candidate = {"format_version": 1, "collections": {"members": contract()}}
    assert preflight_manifest(SimpleNamespace(get=absent), candidate) == candidate
    def denied(*args):
        raise PermissionError("database read permission denied")
    with pytest.raises(PermissionError):
        preflight_manifest(SimpleNamespace(get=denied), candidate)


def test_scan_deletion_checks_use_collection_scoped_state_pages():
    ingestor, state, _ = setup({"members": contract(), "cards": contract()})
    original, prefixes = state.rows, []
    def rows(namespace, prefix=None):
        if namespace == "observations":
            prefixes.append(prefix)
        return original(namespace, prefix=prefix)
    state.rows = rows
    ingestor.scan()
    assert set(prefixes) == {"members:", "cards:"}


def test_source_preflight_checks_every_manifest_collection_without_writes():
    ingestor, _, source = setup({"members": contract(), "system_configs": contract(), "notes": contract()})
    verify_source(source, ingestor.registry.manifest)
    assert all(any(call[0] == "watch" for call in collection.calls) for collection in source.values())


def test_expired_resume_token_records_gap_and_clears_checkpoint_atomically():
    ingestor, state, _ = setup()
    state.capture("old", "members", {"checkpoint_only": True}, {"token": 1})
    ingestor.gap("members")
    assert state.checkpoint("members") == {}
    assert state.get("reconcile", "all")
    assert state.get("health", "coverage_gap")["unresolved"] is True
    assert len(list(state.rows("gaps"))) == 1
    assert len(state.events) == 1  # Only the pre-upgrade checkpoint fixture remains.


def test_scan_keeps_gap_open_until_collection_stream_is_recovered_and_caught_up(monkeypatch):
    ingestor, state, _ = setup()
    at(monkeypatch, "2026-01-01T00:00:01+00:00")
    ingestor.gap("members")
    at(monkeypatch, "2026-01-01T00:00:02+00:00")
    ingestor.scan()
    assert next(state.rows("gaps"))[1]["end"] is None
    state.put("health", "stream:members", {"status": "ready", "caught_up": False, "updated_at": "2026-01-01T00:00:03+00:00"})
    at(monkeypatch, "2026-01-01T00:00:03+00:00")
    ingestor.scan()
    assert next(state.rows("gaps"))[1]["end"] is None
    state.put("health", "stream:members", {"status": "ready", "caught_up": True, "updated_at": "2026-01-01T00:00:04+00:00"})
    at(monkeypatch, "2026-01-01T00:00:04+00:00")
    ingestor.scan()
    assert next(state.rows("gaps"))[1]["end"] == "2026-01-01T00:00:04+00:00"
    assert state.get("health", "coverage_gap")["unresolved"] is False


def test_gap_detected_during_scan_requires_another_scan_and_preserves_hint(monkeypatch):
    ingestor, state, source = setup()
    at(monkeypatch, "2026-01-01T00:00:01+00:00")
    original = source["members"].find
    def find(query):
        at(monkeypatch, "2026-01-01T00:00:02+00:00")
        ingestor.gap("members")
        state.put("health", "stream:members", {"status": "ready", "caught_up": True, "updated_at": "2026-01-01T00:00:02+00:00"})
        return original(query)
    source["members"].find = find
    ingestor.scan()
    assert next(state.rows("gaps"))[1]["end"] is None
    assert state.get("reconcile", "all")["requested_at"] == "2026-01-01T00:00:02+00:00"
