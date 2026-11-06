"""Generation publication is complete, bounded, and never caches authorization."""
from datetime import timedelta
from unittest.mock import patch

from pymongo.errors import ServerSelectionTimeoutError
import pytest

from conftest import oid
from ledger import catalog_cache as cache
from ledger.storage import now


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "true")


def test_snapshot_contains_only_display_metadata_and_selected_shop_payload(env):
    ledger, store, source, *_ = env
    source.data["shops"][0].update(disabled=True, out_of_service=True, internal_note="PRIVATE SHOP NOTE")
    source.data["tools"][0].update(disabled=True, open=True, out_of_service=True, access_code="PRIVATE ACCESS CODE")
    manifest = cache.refresh(ledger)
    result = cache.display_catalog(ledger, "shop1")
    assert result["shops"] == [{"_id": oid(201), "name": "Shop1"}]
    assert len(result["tools"]) == 4
    assert {row["_id"] for row in result["prerequisite_tools"]} == {oid(311)}
    for row in result["shops"] + result["tools"]:
        assert not {"disabled", "open", "out_of_service", "internal_note", "access_code"} & row.keys()
    records = store.select("ledger_catalog", {"generation": manifest["generation"]})
    assert "PRIVATE" not in str(records)
    assert all(row["expires_at"] == manifest["refreshed_at"] + timedelta(days=1) for row in records)
    assert cache.display_catalog(ledger, str(oid(201))) == result
    assert cache.display_catalog(ledger, "unmatched")["tools"] == []


def test_display_fetch_projects_only_skill_labels_and_topology(env):
    ledger, store, source, *_ = env
    source.data["shops"][0]["wiki_url"] = "https://example.com/" + "shop-details" * 1000
    source.data["tools"][0].update(description="LONG DESCRIPTION" * 1000,
                                    wiki_url="https://example.com/" + "tool-details" * 1000)
    manifest = cache.refresh(ledger)
    persisted = store.select("ledger_catalog", {"generation": manifest["generation"], "kind": cache.TOOL_KIND})[0]
    assert "description" in persisted["display_fields"]
    with patch.object(store, "select", wraps=store.select) as reads:
        result = cache.display_catalog(ledger, "Shop1")
    assert all("wiki_url" not in row and "description" not in row for row in result["shops"] + result["tools"])
    assert len(reads.call_args_list) == 2
    assert reads.call_args_list[0].kwargs["projection"] == {"_id": 0, "source_id": 1, "display_fields.name": 1}
    assert reads.call_args_list[1].kwargs["projection"] == {
        "_id": 0, "source_id": 1, "display_fields.name": 1, "display_fields.shop_id": 1,
        "display_fields.prerequisite_ids": 1, "prerequisite_labels": 1}
    assert all(call.kwargs["sort"] == [("source_id", 1)] for call in reads.call_args_list)


def test_refresh_reuses_unchanged_generation_and_republishes_changed_or_near_expiry(env, monkeypatch):
    ledger, store, source, *_ = env
    first = cache.refresh(ledger)
    before = len(store.select("ledger_catalog"))
    with patch.object(store, "put", wraps=store.put) as writes, \
            patch.object(store, "put_many", wraps=store.put_many) as bulk_writes:
        same = cache.refresh(ledger)
    assert same["generation"] == first["generation"]
    assert len(store.select("ledger_catalog")) == before
    assert [call.args[1]["_id"] for call in writes.call_args_list] == [cache.MANIFEST_ID]
    bulk_writes.assert_not_called()
    source.data["tools"][0]["name"] = "Renamed tool"
    changed = cache.refresh(ledger)
    assert changed["generation"] != first["generation"]
    assert cache.display_catalog(ledger, "Shop1")["tools"][0]["name"] == "Renamed tool"
    monkeypatch.setattr(cache, "now", lambda: changed["expires_at"] - timedelta(seconds=330))
    renewed = cache.refresh(ledger)
    assert renewed["generation"] != changed["generation"]


@pytest.mark.parametrize("age", [300, 301])
def test_freshness_boundary(env, monkeypatch, age):
    ledger, *_ = env
    manifest = cache.refresh(ledger)
    monkeypatch.setattr(cache, "now", lambda: manifest["refreshed_at"] + timedelta(seconds=age))
    assert (cache.display_catalog(ledger) is not None) == (age == 300)


def test_freshness_starts_before_source_reads(env, monkeypatch):
    ledger, _, source, *_ = env
    started = now()
    clock = [started]
    original = source.bounded
    def delayed(*args, **kwargs):
        clock[0] = started + timedelta(seconds=30)
        return original(*args, **kwargs)
    monkeypatch.setattr(cache, "now", lambda: clock[0])
    monkeypatch.setattr(source, "bounded", delayed)
    manifest = cache.refresh(ledger)
    assert manifest["refreshed_at"] == started
    clock[0] = started + timedelta(seconds=301)
    assert cache.display_catalog(ledger) is None


def test_catalogs_over_thousand_are_paged_without_global_cap(env, monkeypatch):
    ledger, _, source, *_ = env
    source.data["tools"] = [{"_id": oid(10000 + index), "shop_id": oid(201), "name": f"Tool {index}"}
                            for index in range(1001)]
    queries, original = [], source.bounded
    def record(name, query, fields, limit, timeout_ms=2000):
        queries.append((name, query, fields, limit, timeout_ms))
        return original(name, query, fields, limit, timeout_ms)
    monkeypatch.setattr(source, "bounded", record)
    manifest = cache.refresh(ledger)
    assert manifest["tool_count"] == 1001
    assert len(cache.display_catalog(ledger, "Shop1")["tools"]) == 1001
    tool_pages = [query for name, query, _, _, _ in queries if name == "tools"]
    assert len(tool_pages) == 6 and tool_pages[0] == {}
    assert all("$gt" in query["_id"] for query in tool_pages[1:])
    assert all(limit == 200 and 0 < timeout_ms <= 2000 for _, _, _, limit, timeout_ms in queries)


def test_failed_source_refresh_retains_previous_snapshot(env, monkeypatch):
    ledger, store, source, *_ = env
    first = cache.refresh(ledger)
    original = source.bounded
    def unavailable(name, *args, **kwargs):
        if name == "tools":
            raise ServerSelectionTimeoutError("PRIVATE CONNECTION DATA")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(source, "bounded", unavailable)
    with pytest.raises(ServerSelectionTimeoutError):
        cache.refresh(ledger)
    assert store.get("ledger_catalog", cache.MANIFEST_ID) == first
    assert cache.display_catalog(ledger) is not None


def test_interrupted_writes_never_publish_partial_generation(env, monkeypatch):
    ledger, store, source, *_ = env
    first = cache.refresh(ledger)
    source.data["tools"][0]["name"] = "Different tool"
    original = store.put_many
    def fail_tools(collection, docs):
        if any(doc.get("kind") == cache.TOOL_KIND for doc in docs):
            # A failed/uncertain Mongo bulk can have applied part of its batch.
            original(collection, docs[:2])
            raise OSError("interrupted")
        return original(collection, docs)
    monkeypatch.setattr(store, "put_many", fail_tools)
    with pytest.raises(OSError):
        cache.refresh(ledger)
    assert store.get("ledger_catalog", cache.MANIFEST_ID) == first
    partial = [row for row in store.select("ledger_catalog", {"kind": cache.SHOP_KIND})
               if row["generation"] != first["generation"]]
    assert partial and all("expires_at" in row for row in partial)
    assert cache.display_catalog(ledger) is not None


def test_generation_writes_are_batched_in_two_hundred_record_chunks(env):
    ledger, store, source, *_ = env
    source.data["tools"] = [{"_id": oid(10000 + index), "shop_id": oid(201), "name": f"Tool {index}"}
                            for index in range(401)]
    with patch.object(store, "put_many", wraps=store.put_many) as writes:
        manifest = cache.refresh(ledger)
    assert [len(call.args[1]) for call in writes.call_args_list] == [200, 200, 6]
    assert all(call.args[0] == "ledger_catalog" for call in writes.call_args_list)
    assert manifest["tool_count"] == 401
    assert len(cache.display_catalog(ledger, "Shop1")["tools"]) == 401


def test_deadline_is_checked_between_generation_batches(env, monkeypatch):
    ledger, store, source, *_ = env
    first = cache.refresh(ledger)
    source.data["tools"] = [{"_id": oid(10000 + index), "shop_id": oid(201), "name": f"Tool {index}"}
                            for index in range(401)]
    original_remaining, original_write = cache._remaining, store.put_many
    completed = []
    def remaining(*args, **kwargs):
        if completed:
            raise TimeoutError("refresh deadline")
        return original_remaining(*args, **kwargs)
    def delayed(collection, docs):
        result = original_write(collection, docs)
        completed.append(True)
        return result
    monkeypatch.setattr(cache, "_remaining", remaining)
    monkeypatch.setattr(store, "put_many", delayed)
    with pytest.raises(TimeoutError):
        cache.refresh(ledger)
    assert len(completed) == 1
    assert store.get("ledger_catalog", cache.MANIFEST_ID) == first


@pytest.mark.parametrize("kind", [cache.SHOP_KIND, cache.TOOL_KIND])
def test_missing_generation_rows_trigger_live_fallback_and_rebuild(env, kind):
    ledger, store, *_ = env
    first = cache.refresh(ledger)
    row = store.select("ledger_catalog", {"kind": kind, "generation": first["generation"]})[0]
    # Model server TTL/deletion damage without adding an application remove grant.
    store.data["ledger_catalog"].pop(row["_id"])
    assert cache.display_catalog(ledger, "Shop1") is None
    restored = cache.refresh(ledger)
    assert restored["generation"] != first["generation"]
    assert cache.display_catalog(ledger, "Shop1") is not None


def test_later_refresh_wins_over_older_slow_refresh(env, monkeypatch):
    ledger, store, source, *_ = env
    original, triggered, published = source.bounded, [], []
    clock = [now()]
    monkeypatch.setattr(cache, "now", lambda: clock[0])
    def racing(*args, **kwargs):
        if not triggered:
            triggered.append(True)
            clock[0] += timedelta(seconds=1)
            published.append(cache.refresh(ledger))
        return original(*args, **kwargs)
    monkeypatch.setattr(source, "bounded", racing)
    assert cache.refresh(ledger) is None
    assert store.get("ledger_catalog", cache.MANIFEST_ID) == published[0]


def test_lost_job_lease_cannot_publish(env, monkeypatch):
    ledger, store, source, *_ = env
    cache.schedule_refresh(store)
    job = store.claim("ledger_inbox", kinds=["catalog_refresh"])
    original = source.bounded
    def invalidate(*args, **kwargs):
        current = store.get("ledger_inbox", cache.REFRESH_JOB_ID)
        current["lease"] = "new-lease"
        store.put("ledger_inbox", current)
        return original(*args, **kwargs)
    monkeypatch.setattr(source, "bounded", invalidate)
    assert cache.refresh(ledger, job) is None
    assert store.get("ledger_catalog", cache.MANIFEST_ID) is None


def test_schedule_coalesces_pending_and_working_then_reopens_terminal_job(env):
    _, store, *_ = env
    first = cache.schedule_refresh(store)
    assert cache.schedule_refresh(store) == first
    working = store.claim("ledger_inbox", kinds=["catalog_refresh"])
    assert cache.schedule_refresh(store) == working
    working["status"] = "done"
    store.put("ledger_inbox", working)
    reopened = cache.schedule_refresh(store)
    assert reopened["status"] == "pending" and reopened["attempts"] == 0
    assert "lease" not in reopened
    assert store.count("ledger_inbox", {"kind": "catalog_refresh"}) == 1


def test_cache_switch_and_read_failures_use_live_fallback(env, monkeypatch):
    ledger, store, *_ = env
    cache.refresh(ledger)
    with patch.object(store, "select", side_effect=ServerSelectionTimeoutError("PRIVATE")):
        assert cache.display_catalog(ledger) is None
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "false")
    with patch.object(store, "get", side_effect=AssertionError("disabled cache read")):
        assert cache.display_catalog(ledger) is None
        assert cache.refresh(ledger) is None
        assert cache.schedule_refresh(store) is None


def test_empty_snapshot_and_literal_shop_search(env):
    ledger, _, source, *_ = env
    source.data["shops"] = []
    source.data["tools"] = []
    cache.refresh(ledger)
    assert cache.display_catalog(ledger)["shops"] == []
    assert cache.display_catalog(ledger)["tools"] == []
