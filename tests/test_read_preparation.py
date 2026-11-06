"""Preparation is additive; shadow reports expose labels rather than member data."""
from copy import deepcopy
import json
import os

import pytest

from conftest import oid
from ledger.catalog_cache import MANIFEST_ID
from ledger.domain import Denied
from ledger.query_tools import QueryTools
from ledger.quests import Quests
from ledger.read_options import optimized_reads
from ledger.read_preparation import prepare, verify
from test_quest_discovery import install


def member(n):
    return str(oid(n))


def test_preparation_backfills_idempotently_without_source_or_game_mutations(joined, monkeypatch):
    ledger, store, source, _, api, slack = joined
    q = install(joined)
    store.put("ledger_catalog", {"_id": "quest-head:" + q["logical_id"], "revision": q["_id"]})
    store.put("ledger_relationships", {"_id": f"acceptance:{member(1)}:{q['logical_id']}",
        "kind": "quest_acceptance", "member_id": member(1), "quest_revision": q["_id"], "logical_id": q["logical_id"]})
    store.put("ledger_context", {"_id": "edited-context", "kind": "message", "at": "123.456",
        "text": "Already edited body", "edited_at": "124.000", "member_id": member(1)})
    source_before = deepcopy(source.data)
    preserved = {collection: deepcopy(documents) for collection, documents in store.data.items()
                 if collection not in ("ledger_catalog", "ledger_context", "ledger_relationships")}
    indexes = []
    monkeypatch.setattr(store, "indexes", lambda: indexes.append(True), raising=False)
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "false")
    first = prepare(ledger)
    generation = store.get("ledger_catalog", MANIFEST_ID)["generation"]
    second = prepare(ledger)
    assert len(indexes) == 2
    assert first["quest_heads"]["heads"] == 1 and first["quest_heads"]["acceptances"] == 1
    assert first["context_order"] == 1
    assert second["quest_heads"] == {"heads": 0, "acceptances": 0, "display_titles": 0}
    assert second["context_order"] == 0
    assert first["catalog"] == second["catalog"] == {"available": True, "shops": 5, "tools": 20}
    assert store.get("ledger_catalog", MANIFEST_ID)["generation"] == generation
    context = store.get("ledger_context", "edited-context")
    assert context["text"] == "Already edited body" and context["edited_at"] == "124.000"
    assert context["at_order"] == 123.456
    assert source.data == source_before
    assert all(store.data[collection] == documents for collection, documents in preserved.items())
    assert os.environ["LEDGER_OPTIMIZED_READS"] == "false"
    api.complete.assert_not_called()
    api.tool_response.assert_not_called()
    assert slack.mock_calls == []


def shadow_stubs(monkeypatch):
    import ledger.skills as skills
    monkeypatch.setattr(skills, "highest_skill", lambda *args: {"depth": 0})
    monkeypatch.setattr(skills, "skill_summary", lambda *args: {"shops": []})
    monkeypatch.setattr(Quests, "options", lambda *args: [])
    monkeypatch.setattr(QueryTools, "query", lambda self, args: {
        "collection": args["collection"], "results": [], "retrieved_at": "new" if optimized_reads() else "old"})


def test_shadow_verification_ignores_retrieval_time_and_is_read_only(joined, monkeypatch):
    ledger, store, source, _, api, slack = joined
    shadow_stubs(monkeypatch)
    owned_before, source_before = deepcopy(store.data), deepcopy(source.data)
    monkeypatch.delenv("LEDGER_OPTIMIZED_READS", raising=False)
    report = verify(ledger, sample_size=2)
    assert report["cases"] == 17 and report["mismatches"] == [] and report["skipped"] == 0
    assert len(report["commands"]) == 34
    assert "LEDGER_OPTIMIZED_READS" not in os.environ
    assert store.data == owned_before and source.data == source_before
    api.complete.assert_not_called()
    api.tool_response.assert_not_called()
    assert slack.mock_calls == []


def test_shadow_mismatch_report_contains_labels_without_identity_or_body(joined, monkeypatch):
    ledger, *_ = joined
    shadow_stubs(monkeypatch)
    secret = "DO_NOT_EXPORT_PRIVATE_MEMBER_TEXT"
    monkeypatch.setattr(QueryTools, "query", lambda self, args: {
        "collection": args["collection"], "results": [{"member_id": member(1), "body": secret,
            "value": 1 if optimized_reads() else 0}], "retrieved_at": "changes"})
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "false")
    report = verify(ledger, sample_size=1)
    assert report["mismatches"] == ["sample-0:" + collection for collection in
                                    ("shops", "tools", "tool_checkouts", "volunteer_tasks", "volunteer_events")]
    serialized = json.dumps(report)
    assert secret not in serialized and member(1) not in serialized
    assert "U1" not in serialized and "results" not in serialized
    assert os.environ["LEDGER_OPTIMIZED_READS"] == "false"


@pytest.mark.parametrize("denied_mode", [True, False])
def test_shadow_verification_reports_one_sided_denial_as_mismatch(joined, monkeypatch, denied_mode):
    import ledger.skills as skills
    ledger, *_ = joined
    shadow_stubs(monkeypatch)
    def skill(*args):
        if optimized_reads() is denied_mode:
            raise Denied("Private denial detail")
        return {"depth": 0}
    monkeypatch.setattr(skills, "highest_skill", skill)
    report = verify(ledger, sample_size=1)
    assert "sample-0:highest-skill" in report["mismatches"]
    assert report["skipped"] == 0
    assert "Private denial detail" not in json.dumps(report)


def test_shadow_verification_skips_only_matching_denial(joined, monkeypatch):
    import ledger.skills as skills
    ledger, *_ = joined
    shadow_stubs(monkeypatch)
    def denied(*args):
        raise Denied("Ineligible sample")
    monkeypatch.setattr(skills, "highest_skill", denied)
    report = verify(ledger, sample_size=1)
    assert report["skipped"] == 1
    assert report["mismatches"] == []


@pytest.mark.parametrize("large_collection", ["shops", "tools"])
def test_shadow_verifier_recognizes_real_legacy_catalog_bound_removal(joined, monkeypatch, large_collection):
    ledger, store, source, _, api, slack = joined
    real_query = QueryTools.query
    shadow_stubs(monkeypatch)
    monkeypatch.setattr(QueryTools, "query", real_query)
    missing = 1001 - len(source.data[large_collection])
    for i in range(missing):
        doc = {"_id": oid(10000 + i), "name": f"Additional catalog item {i}",
               "description": "SOURCE_BODY_MUST_NOT_APPEAR_IN_SHADOW_REPORT"}
        if large_collection == "tools":
            doc.update(shop_id=oid(201), prerequisite_ids=[])
        source.data[large_collection].append(doc)
    source_before, owned_before = deepcopy(source.data), deepcopy(store.data)
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "false")
    report = verify(ledger, sample_size=1)
    expected_collections = ("shops", "tools", "tool_checkouts", "volunteer_tasks", "volunteer_events") if large_collection == "shops" else ("tool_checkouts",)
    assert report["expected_differences"] == [f"sample-0:{collection}:legacy-catalog-bound" for collection in expected_collections]
    assert report["mismatches"] == []
    assert source.data == source_before and store.data == owned_before
    assert "SOURCE_BODY_MUST_NOT_APPEAR_IN_SHADOW_REPORT" not in json.dumps(report)
    assert os.environ["LEDGER_OPTIMIZED_READS"] == "false"
    api.complete.assert_not_called()
    api.tool_response.assert_not_called()
    assert slack.mock_calls == []


def test_shadow_query_errors_are_public_mismatch_labels_without_exception_content(joined, monkeypatch):
    ledger, *_ = joined
    shadow_stubs(monkeypatch)
    def failed(*args):
        raise ValueError("DO_NOT_REPORT_PRIVATE_SOURCE_ERROR " + member(1))
    monkeypatch.setattr(QueryTools, "query", failed)
    report = verify(ledger, sample_size=1)
    assert report["mismatches"] == ["sample-0:" + collection + ":error" for collection in
                                    ("shops", "tools", "tool_checkouts", "volunteer_tasks", "volunteer_events")]
    assert report["expected_differences"] == []
    assert "DO_NOT_REPORT_PRIVATE_SOURCE_ERROR" not in json.dumps(report)
    assert member(1) not in json.dumps(report)
