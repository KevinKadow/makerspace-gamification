"""Behavioral tests for bounded owned reads, private history, and metrics."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from conftest import oid
from ledger.context_reads import backfill_context_order, history
from ledger.storage import MemoryStore, MongoStore, now


def member(n):
    return str(oid(n))


def test_memory_reads_bound_sort_project_and_preserve_complete_documents():
    store = MemoryStore()
    original = [{"_id": "a", "rank": 2, "private": "body", "nested": {"score": 7, "note": "secret"}},
                {"_id": "b", "rank": 3, "private": "body", "nested": {"score": 5}},
                {"_id": "c", "rank": 3, "private": "body", "nested": {"score": 9}}]
    for doc in original:
        store.put("ledger_catalog", doc)
    rows = store.select("ledger_catalog", {"rank": {"$gte": 2}}, projection={"rank": 1, "nested.score": 1},
                        sort=[("rank", -1), ("nested.score", 1)], limit=2, max_time_ms=2000)
    assert rows == [{"_id": "b", "rank": 3, "nested": {"score": 5}},
                    {"_id": "c", "rank": 3, "nested": {"score": 9}}]
    rows[0]["nested"]["score"] = 99
    assert store.get("ledger_catalog", "b")["nested"]["score"] == 5
    assert store.get("ledger_catalog", "a", {"nested.note": 0, "private": 0}) == {
        "_id": "a", "rank": 2, "nested": {"score": 7}}
    assert store.get("ledger_catalog", "a", {"_id": 0, "rank": 1}) == {"rank": 2}
    assert store.count("ledger_catalog", {"rank": {"$gt": 2}}, max_time_ms=2000) == 2
    assert store.exists("ledger_catalog", {"nested.score": 9})
    assert not store.exists("ledger_catalog", {"rank": 99})
    assert not store.get("ledger_catalog", "missing", {"rank": 1})
    assert store.select("ledger_catalog", sort=[("_id", 1)]) == original


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_owned_read_limits_reject_unbounded_or_invalid_values(limit):
    for store in (MemoryStore(), MongoStore(MagicMock())):
        with pytest.raises(ValueError, match="positive integers"):
            store.select("ledger_catalog", limit=limit)


def test_memory_scope_array_filters_match_mongo_membership_semantics():
    store = MemoryStore()
    store.put("ledger_relationships", {"_id": "scoped", "scope": {"shops": ["shop-a", "shop-b"]}})
    store.put("ledger_relationships", {"_id": "other", "scope": {"shops": ["shop-c"]}})
    assert [row["_id"] for row in store.select("ledger_relationships", {"scope.shops": {"$in": ["shop-b"]}})] == ["scoped"]
    assert [row["_id"] for row in store.select("ledger_relationships", {"scope.shops": {"$nin": ["shop-b"]}})] == ["other"]


def test_mongo_read_options_and_aggregations_propagate_session():
    database, session, cursor = MagicMock(), object(), MagicMock()
    collection = database.__getitem__.return_value
    collection.find.return_value = cursor
    for name in ("sort", "limit", "max_time_ms"):
        getattr(cursor, name).return_value = cursor
    cursor.__iter__.return_value = iter([{"_id": "a"}])
    store = MongoStore(database, session)
    assert store.select("ledger_catalog", {"kind": "test"}, projection={"title": 1},
                        sort=[("title", 1)], limit=32, max_time_ms=2000) == [{"_id": "a"}]
    collection.find.assert_called_once_with({"kind": "test"}, {"title": 1}, session=session)
    cursor.sort.assert_called_once_with([("title", 1)])
    cursor.limit.assert_called_once_with(32)
    cursor.max_time_ms.assert_called_once_with(2000)
    store.get("ledger_catalog", "a", {"title": 1})
    collection.find_one.assert_called_with({"_id": "a"}, {"title": 1}, session=session)
    collection.count_documents.return_value = 4
    assert store.count("ledger_catalog", {"kind": "test"}, max_time_ms=1500) == 4
    collection.count_documents.assert_called_once_with({"kind": "test"}, session=session, maxTimeMS=1500)
    store.exists("ledger_catalog", {"kind": "test"})
    collection.find_one.assert_called_with({"kind": "test"}, {"_id": 1}, session=session)
    pipeline = [{"$match": {"kind": "test"}}, {"$count": "total"}]
    collection.aggregate.return_value = [{"total": 4}]
    assert store.aggregate("ledger_catalog", pipeline) == [{"total": 4}]
    collection.aggregate.assert_called_once_with(pipeline, session=session, maxTimeMS=2000, allowDiskUse=False)


def context(store, timestamp, author=2, text=None, **changes):
    doc = {"_id": f"history:{timestamp:03}", "kind": "message", "member_id": member(author),
           "channel": "CCHAT", "thread": "root", "at": str(timestamp), "at_order": float(timestamp),
           "text": text or f"Observed result {timestamp}", "conversation_requested": True,
           "consent_generation": 0, "participating": True, "expires_at": now() + timedelta(days=1), **changes}
    store.put("ledger_context", doc)
    return doc


def payload(**changes):
    return {"member_id": member(1), "channel": "CCHAT", "thread": "root", "message_id": "current",
            "consent_generation": 0, "participating": True, **changes}


def test_history_pages_past_denied_authors_until_six_authorized_messages(joined, monkeypatch):
    ledger, store, source, *_ = joined
    source.data["members"][3]["status"] = "suspended"
    for i in range(1, 7):
        context(store, i)
    for i in range(7, 47):
        context(store, i, author=4)
    context(store, 47, author=1, consent_generation=1)
    future_name = store.get("ledger_catalog", "rank_display")["ranks"][-1]["name"]
    context(store, 48, text=f"The next future rank is {future_name}")
    context(store, 49, _id="current")
    context(store, 50, expires_at=now() - timedelta(seconds=1))
    context(store, 51, thread="another-thread")
    page_sizes, author_batches = [], []
    original_select, original_identities = store.select, source.identities
    def select(collection, query=None, **kwargs):
        result = original_select(collection, query, **kwargs)
        if collection == "ledger_context":
            assert kwargs["limit"] == 32
            assert kwargs["max_time_ms"] == 2000
            page_sizes.append(len(result))
        return result
    def identities(ids):
        author_batches.append(set(ids))
        return original_identities(ids)
    monkeypatch.setattr(store, "select", select)
    monkeypatch.setattr(source, "identities", identities)
    assert history(ledger, payload()) == [{"role": "user", "content": f"Author U2: Observed result {i}"}
                                          for i in range(1, 7)]
    assert page_sizes == [32, 17]
    # restriction_filter performs its own caller gate before history paging.
    flattened = [m for batch in author_batches[-len(page_sizes):] for m in batch]
    assert len(flattened) == len(set(flattened))


def test_history_rejects_ambiguous_identity_and_unrequested_or_old_consent(joined):
    ledger, store, source, *_ = joined
    context(store, 1, text="First permitted request")
    context(store, 2, author=1, text="Old participation", participating=False)
    context(store, 3, author=1, text="Old consent", consent_generation=99)
    context(store, 4, text="Unrequested ambient body", conversation_requested=False)
    context(store, 5, author=4, text="Ambiguous person")
    context(store, 6, kind="reply", text="Saved assistant reply")
    source.data["slack_users"].append({"_id": oid(999), "member_id": oid(5), "slack_id": "U4"})
    assert history(ledger, payload(), unrelated=True) == [
        {"role": "user", "content": "Author U2: First permitted request"},
        {"role": "assistant", "content": "Saved assistant reply"}]


def test_history_tied_timestamps_page_without_losing_older_permitted_records(joined):
    ledger, store, source, *_ = joined
    source.data["members"][3]["status"] = "revoked"
    for i in range(1, 7):
        context(store, i, at="100.000001", at_order=100.000001)
    for i in range(7, 47):
        context(store, i, author=4, at="100.000001", at_order=100.000001)
    assert history(ledger, payload()) == [{"role": "user", "content": f"Author U2: Observed result {i}"}
                                        for i in range(1, 7)]


def test_direct_history_covers_threads_in_the_same_channel(joined):
    ledger, store, *_ = joined
    context(store, 1, channel="D1", thread="old-thread")
    context(store, 2, channel="D1", thread="new-thread")
    context(store, 3, channel="DOTHER")
    assert history(ledger, payload(channel="D1", thread="new-thread")) == [
        {"role": "user", "content": "Author U2: Observed result 1"},
        {"role": "user", "content": "Author U2: Observed result 2"}]


def test_history_backfill_is_idempotent_preserves_edits_and_deleted_records(joined, monkeypatch):
    _, store, *_ = joined
    for i in range(1, 4):
        doc = context(store, i)
        doc.pop("at_order")
        store.put("ledger_context", doc)
    original_atomic = store.atomic
    calls = 0
    def concurrent(fn):
        nonlocal calls
        calls += 1
        if calls == 1:
            edited = store.get("ledger_context", "history:001")
            edited["text"] = "Edited message body"
            store.put("ledger_context", edited)
            store.delete("ledger_context", "history:002")
        return original_atomic(fn)
    monkeypatch.setattr(store, "atomic", concurrent)
    assert backfill_context_order(store) == 2
    assert store.get("ledger_context", "history:001")["text"] == "Edited message body"
    assert store.get("ledger_context", "history:001")["at_order"] == 1.0
    assert store.get("ledger_context", "history:002") is None
    assert backfill_context_order(store) == 0


def test_legacy_history_fallback_remains_authorized_and_numeric(joined, monkeypatch):
    ledger, store, *_ = joined
    for i in (2, 10):
        doc = context(store, i)
        doc.pop("at_order")
        store.put("ledger_context", doc)
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "true")
    optimized = history(ledger, payload())
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "false")
    assert history(ledger, payload()) == optimized == [
        {"role": "user", "content": "Author U2: Observed result 2"},
        {"role": "user", "content": "Author U2: Observed result 10"}]


def test_metrics_grouped_snapshot_matches_legacy_with_mixed_records(joined, monkeypatch):
    from ledger.metrics import snapshot, _legacy_snapshot
    _, store, *_ = joined
    person = store.get("ledger_participants", member(1))
    person["metrics"] = {"mentoring": 2}
    store.put("ledger_participants", person)
    evidence = [
        {"_id": "learning", "kind": "submission", "status": "approved", "achievement": "challenge"},
        {"_id": "feedback", "kind": "feedback", "text": "private response"},
        {"_id": "generation", "kind": "quest_generation", "status": "pending"},
        {"_id": "decision", "kind": "ai_decision", "category": "imitation_deduction", "status": "committed"},
        {"_id": "rejection", "kind": "ai_rejection", "reason_code": "budget"},
        {"_id": "audit", "kind": "delegation_audit", "action": "revoked"},
        {"_id": "arrival", "kind": "arrival", "status": "skipped"}]
    for doc in evidence:
        store.put("ledger_evidence", doc)
    for identifier, status, composed in [("done", "done", {"outcome": "generated", "latency": 0.25}),
                                         ("pending", "pending", {"outcome": "fallback", "latency": 0.5})]:
        store.put("ledger_outbox", {"_id": identifier, "kind": "message", "status": status,
            "payload": {"audience": "shared", "body": "private body"}, "composed": composed})
    store.put("ledger_inbox", {"_id": "engagement", "kind": "engagement", "status": "working", "last_error": "Denied"})
    store.put("ledger_projects", {"_id": "project", "description": "Private project draft"})
    store.put("ledger_quests", {"_id": "group", "status": "completed"})
    store.put("ledger_relationships", {"_id": "shared", "kind": "quest_project", "status": "completed"})
    expected = _legacy_snapshot(store)
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "true")
    narrow_reads, grouped_reads = [], []
    def select(*args, **kwargs):
        narrow_reads.append((args, kwargs))
        return store.select(*args, **kwargs)
    def aggregate(collection, pipeline, **kwargs):
        grouped_reads.append((collection, pipeline, kwargs))
        return store.aggregate(collection, pipeline, **kwargs)
    proxy = SimpleNamespace(select=select, aggregate=aggregate, count=store.count)
    assert snapshot(proxy) == expected
    base_groups = [read for read in grouped_reads if "$match" not in read[1][0]]
    result_groups = [read for read in grouped_reads if "$match" in read[1][0]]
    assert len(base_groups) == 4
    assert len(result_groups) == 3  # Short generation, delivery timings, and summary owner totals.
    assert all(any("$group" in stage for stage in pipeline) for _, pipeline, _ in grouped_reads)
    assert len(narrow_reads) == 1
    assert narrow_reads[0][1]["projection"] == {"_id": 0, "composed.latency": 1}
    assert snapshot(MemoryStore()) == _legacy_snapshot(MemoryStore())


@pytest.mark.parametrize("setting, expected", [("true", True), ("TRUE", True), ("1", True),
                                              ("yes", True), ("false", False), ("0", False)])
def test_optimized_read_rollout_switch(monkeypatch, setting, expected):
    from ledger.read_options import optimized_reads
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", setting)
    assert optimized_reads() is expected


def test_command_listener_counts_wire_results_without_retaining_filters_or_bodies(caplog, monkeypatch):
    from bson import BSON
    from ledger import read_metrics
    listener = read_metrics.ReadMetrics()
    secret = "DO_NOT_RETAIN_SOURCE_BODY_OR_CREDENTIAL"
    def event(command_name, request_id, **values):
        return SimpleNamespace(command_name=command_name, request_id=request_id,
                               connection_id=("mongo.test", 27017), **values)
    listener.started(event("find", 1, command={"find": "members", "filter": {"private": secret}}))
    assert secret not in repr(listener.pending)
    reply = {"cursor": {"id": 5, "ns": "private_database.members", "firstBatch": [
        {"_id": 1, "private": secret}, {"_id": 2, "body": secret}]}, "ok": 1}
    listener.succeeded(event("find", 1, reply=reply, duration_micros=120))
    listener.started(event("getMore", 2, command={"getMore": 5, "collection": "members", "comment": secret}))
    more = {"cursor": {"id": 0, "nextBatch": [{"_id": 3, "body": secret}]}, "ok": 1}
    listener.succeeded(event("getMore", 2, reply=more, duration_micros=80))
    listener.started(event("aggregate", 3, command={"aggregate": "ledger_context", "pipeline": [{"$match": {"text": secret}}]}))
    listener.failed(event("aggregate", 3, failure={"errmsg": secret}, duration_micros=40))
    values = listener.snapshot()
    assert values["find:members"] == {"commands": 1, "documents": 2, "bytes": len(BSON.encode(reply)),
                                     "duration_us": 120, "failures": 0}
    assert values["getMore:members"]["documents"] == 1
    assert values["aggregate:ledger_context"]["failures"] == 1
    assert listener.pending == {}
    assert secret not in repr(values) and "private_database" not in repr(values)
    monkeypatch.setattr(read_metrics, "READ_METRICS", listener)
    caplog.set_level("INFO", logger="ledger.read_metrics")
    read_metrics.log_metrics()
    assert "documents=2" in caplog.text and "failures=1" in caplog.text
    assert secret not in caplog.text and "private_database" not in caplog.text
    assert listener.snapshot() == {}
