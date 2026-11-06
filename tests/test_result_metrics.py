import json
from unittest.mock import patch

import pytest

from ledger.metrics import snapshot
from ledger.storage import MemoryStore


def snapshots(store, monkeypatch):
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "false")
    legacy = snapshot(store)
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "true")
    optimized = snapshot(store)
    assert optimized == legacy
    return optimized


def job(store, key, kind="summary_delivery", composed=None, delivery_metrics=None):
    row = {"_id": key, "kind": kind, "status": "done", "payload": {"facts": "PRIVATE_FACTS"}}
    if composed is not None:
        row["composed"] = {"outcome": "generated", "text": "PRIVATE_MESSAGE", **composed}
    if delivery_metrics is not None:
        row["delivery_metrics"] = delivery_metrics
    store.put("ledger_outbox", row)


def test_result_metrics_empty_legacy_and_optimized_agree(monkeypatch):
    result = snapshots(MemoryStore(), monkeypatch)
    assert result["short_generation"] == {"attempts": 0, "profiles": {}, "fallback_reasons": {},
                                          "generation_ms": {"count": 0, "total": 0, "mean": 0}}
    assert result["result_notifications"] == {"actions": 0, "posts": 0, "updates": 0, "operations": {},
        "queue_age_ms": {"count": 0, "total": 0, "mean": 0},
        "summary_latency_ms": {"count": 0, "total": 0, "mean": 0}}


def test_result_metrics_count_authoritative_owner_posts_and_all_short_profiles(monkeypatch):
    store = MemoryStore()
    store.put("ledger_evidence", {"_id": "one", "kind": "notification_summary", "status": "delivered",
        "post_count": 1, "update_count": 2, "events": ["PRIVATE_EVENT"]})
    store.put("ledger_evidence", {"_id": "two", "kind": "notification_summary", "status": "pending",
        "post_count": 2, "update_count": 1})
    store.put("ledger_evidence", {"_id": "old-empty", "kind": "notification_summary", "status": "pending"})
    for key, profile, elapsed, reason, operation, age, latency in (
        ("post", "receipt", 10, None, "post", 100, 200),
        ("update", "receipt", 20, "transport_failure", "update", 200, 400),
        ("replace", "summary", 30, "invalid_output", "post_and_update", 300, 600),
    ):
        job(store, key, composed={"generation_profile": profile, "generation_ms": elapsed,
            "fallback_reason": reason, "outcome": "fallback" if reason else "generated"},
            delivery_metrics={"operation": operation, "queue_age_ms": age, "summary_latency_ms": latency,
                              "post_count": 999, "update_count": 999})
    job(store, "guidance", kind="guidance", composed={"generation_profile": "guidance", "generation_ms": 40,
                                                     "fallback_reason": "circuit_open", "outcome": "fallback"})
    job(store, "old", kind="message", composed={"latency": 0.25, "fallback_reason": "PRIVATE_LEGACY_ERROR"})
    result = snapshots(store, monkeypatch)
    assert result["short_generation"] == {"attempts": 4, "profiles": {"receipt": 2, "summary": 1, "guidance": 1},
        "fallback_reasons": {"transport_failure": 1, "invalid_output": 1, "circuit_open": 1},
        "generation_ms": {"count": 4, "total": 100, "mean": 25}}
    assert result["result_notifications"] == {"actions": 3, "posts": 3, "updates": 3,
        "operations": {"post": 1, "update": 1, "post_and_update": 1},
        "queue_age_ms": {"count": 3, "total": 600, "mean": 200},
        "summary_latency_ms": {"count": 3, "total": 1200, "mean": 400}}
    assert result["inference_latency_seconds"] == [0.25]
    assert "PRIVATE_" not in json.dumps(result)


def test_unknown_reasons_and_operations_are_bounded_without_text_leak(monkeypatch):
    store = MemoryStore()
    for index in range(50):
        job(store, str(index), composed={"generation_profile": "receipt", "generation_ms": 1.25,
            "fallback_reason": "PRIVATE_ERROR_" + str(index), "outcome": "fallback"},
            delivery_metrics={"operation": "PRIVATE_OPERATION_" + str(index), "queue_age_ms": 1})
    result = snapshots(store, monkeypatch)
    assert result["short_generation"]["fallback_reasons"] == {"other": 50}
    assert result["short_generation"]["generation_ms"] == {"count": 50, "total": 62.5, "mean": 1.25}
    assert result["result_notifications"]["operations"] == {"other": 50}
    assert result["result_notifications"]["summary_latency_ms"]["count"] == 0
    assert "PRIVATE_" not in json.dumps(result)


def test_missing_historical_diagnostics_do_not_add_latency_samples(monkeypatch):
    store = MemoryStore()
    store.put("ledger_evidence", {"_id": "owner", "kind": "notification_summary", "status": "delivered",
                                  "post_count": None, "update_count": None})
    job(store, "old-short", composed={"generation_profile": "summary"}, delivery_metrics={"operation": "post"})
    job(store, "not-delivered", composed={"generation_profile": "guidance"})
    result = snapshots(store, monkeypatch)
    assert result["short_generation"]["attempts"] == 2
    assert result["short_generation"]["generation_ms"]["count"] == 0
    assert result["result_notifications"]["posts"] == 0
    assert result["result_notifications"]["queue_age_ms"]["count"] == 0


def test_optimized_diagnostics_use_bounded_aggregates_and_narrow_existing_latency_read(monkeypatch):
    store = MemoryStore()
    job(store, "short", composed={"generation_profile": "guidance", "generation_ms": 2})
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "true")
    original_select = store.select
    aggregates = []
    aggregate_running = [False]
    original_aggregate = store.aggregate
    def aggregate(collection, pipeline, **kwargs):
        aggregates.append((collection, pipeline, kwargs))
        aggregate_running[0] = True
        try:
            return original_aggregate(collection, pipeline, **kwargs)
        finally:
            aggregate_running[0] = False
    def select(collection, query=None, **kwargs):
        if not aggregate_running[0]:
            assert collection == "ledger_outbox"
            assert kwargs["projection"] == {"_id": 0, "composed.latency": 1}
        return original_select(collection, query, **kwargs)
    with patch.object(store, "aggregate", side_effect=aggregate), patch.object(store, "select", side_effect=select):
        result = snapshot(store)
    assert result["short_generation"]["attempts"] == 1
    new_groups = [(collection, pipeline, kwargs) for collection, pipeline, kwargs in aggregates if "$match" in pipeline[0]]
    assert len(new_groups) == 3
    assert all(kwargs == {"max_time_ms": 2000} for _, _, kwargs in new_groups)
    assert all(pipeline[-1]["$group"]["count"] == {"$sum": 1} for _, pipeline, _ in new_groups)
    assert "$push" not in json.dumps(new_groups)
    assert "PRIVATE_" not in json.dumps(result)
