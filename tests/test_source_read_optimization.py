"""Fixed source reads preserve eligibility while bounding transferred records."""
from copy import deepcopy
from contextlib import nullcontext
from datetime import timedelta
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from pymongo import MongoClient, timeout
from pymongo.errors import OperationFailure
from bson.int64 import Int64

from conftest import oid
from ledger.catalog_cache import display_catalog, refresh
from ledger.query_tools import QueryTools
from ledger.skills import highest_skill, skill_summary
from ledger.source_reads import (CATALOG_FIELDS, SKILL_TOOL_FIELDS, catalog_pipeline,
                                 identity_pipeline, skill_ancestor_pipeline, skill_graph_pipeline)
from ledger.sources import MemorySources, Sources, sid
from ledger.storage import MemoryStore, now


@pytest.fixture(autouse=True)
def optimized(monkeypatch):
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "true")


def test_large_catalog_returns_only_bounded_matches(joined):
    ledger, _, source, *_ = joined
    source.data["shops"].extend({"_id": oid(2000 + i), "name": f"Unrelated {i}"} for i in range(1500))
    source.data["tools"].extend({"_id": oid(5000 + i), "shop_id": oid(201), "name": f"Unrelated {i}"} for i in range(1500))
    with patch.object(source, "catalog_query", wraps=source.catalog_query) as catalog, patch.object(source, "bounded", wraps=source.bounded) as bounded:
        result = QueryTools(ledger, sid(oid(1))).query({"collection": "tools", "search": "Tool1", "limit": 2})
    assert [row["name"] for row in result["results"]] == ["Tool1-1", "Tool1-2"]
    assert result["truncated"]
    assert catalog.call_count == bounded.call_count == 1
    assert bounded.call_args.args[0] == "tools" and bounded.call_args.args[3] == 3


@pytest.mark.parametrize("filters", [
    {"collection": "shops"}, {"collection": "tools", "search": "Tool"},
    {"collection": "tools", "shop_id": sid(oid(201)), "out_of_service": False},
    {"collection": "tool_checkouts"}, {"collection": "tool_checkouts", "search": "1-1"},
    {"collection": "volunteer_tasks"}, {"collection": "volunteer_events"},
])
def test_catalog_matches_legacy_eligibility_and_projection(joined, monkeypatch, filters):
    ledger, _, source, *_ = joined
    source.data["shops"][1]["disabled"] = True
    source.data["tools"][1]["disabled"] = True
    source.data["tools"][2]["out_of_service"] = True
    source.data["tool_checkouts"] = [
        {"_id": oid(700), "member_id": oid(1), "tool_id": oid(311), "internal_notes": "SECRET"},
        {"_id": oid(701), "member_id": oid(1), "tool_id": oid(321)},
        {"_id": oid(702), "member_id": oid(2), "tool_id": oid(311)},
        {"_id": oid(703), "member_id": oid(1), "tool_id": oid(314), "revoked_at": now()},
    ]
    source.data["volunteer_tasks"] = [
        {"_id": oid(710), "title": "Unscoped", "status": "available", "internal_notes": "SECRET"},
        {"_id": oid(711), "title": "Disabled parent", "status": "available", "shop_id": oid(202)},
        {"_id": oid(712), "title": "Closed", "status": "closed"},
    ]
    source.data["volunteer_events"] = [
        {"_id": oid(720), "title": "Unscheduled", "status": "open"},
        {"_id": oid(721), "title": "Future", "status": "open", "event_date": now() + timedelta(days=3)},
        {"_id": oid(722), "title": "Past", "status": "open", "event_date": now() - timedelta(days=3)},
    ]
    result = QueryTools(ledger, sid(oid(1))).query(filters)
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "false")
    legacy = QueryTools(ledger, sid(oid(1))).query(filters)
    assert result["results"] == legacy["results"]
    assert result["truncated"] == legacy["truncated"]
    assert "SECRET" not in str(result)


def test_catalog_filters_disabled_parent_before_limit(joined):
    ledger, _, source, *_ = joined
    source.data["shops"][0]["disabled"] = True
    result = QueryTools(ledger, sid(oid(1))).query({"collection": "tools", "limit": 1})
    assert result["results"][0]["shop_id"] == sid(oid(202))
    assert result["truncated"]


def test_catalog_pipelines_project_after_filter_and_limit():
    database = MagicMock()
    database.__getitem__.return_value.aggregate.return_value = []
    source = Sources(database)
    for name in CATALOG_FIELDS:
        source.catalog_query(name, member_id=oid(1), boundary=now(), limit=3)
        pipeline = database.__getitem__.return_value.aggregate.call_args.args[0]
        assert pipeline[-3:] == [{"$sort": {"_id": 1}}, {"$limit": 3},
                                 {"$project": {"_id": 1, **dict.fromkeys(CATALOG_FIELDS[name].split(), 1)}}]
        assert database.__getitem__.return_value.aggregate.call_args.kwargs == {"maxTimeMS": 2000, "allowDiskUse": False}
    assert database.__getitem__.return_value.aggregate.call_count == 5
    database.__getitem__.return_value.find.assert_not_called()
    with pytest.raises(ValueError):
        catalog_pipeline("members", limit=3)
    with pytest.raises(ValueError):
        catalog_pipeline("shops", limit=27)


def test_empty_projection_is_id_only_not_full_document():
    database = MagicMock()
    source = Sources(database)
    source.members_by_id([oid(1)], fields=[])
    assert database.__getitem__.return_value.find.call_args.args[1] == {"_id": 1}
    with pytest.raises(ValueError):
        source.members_by_id([oid(1)], fields=["billing_secret"])


@pytest.mark.parametrize("change", ["member_duplicate", "uid_duplicate", "malformed", "merged"])
def test_batched_identity_remains_bilaterally_unique(env, monkeypatch, change):
    source = env[2]
    if change == "member_duplicate":
        source.data["slack_users"].append({"_id": oid(800), "member_id": oid(1), "slack_id": "UOTHER"})
    elif change == "uid_duplicate":
        # The conflicting owner is outside the requested cohort.
        source.data["slack_users"].append({"_id": oid(800), "member_id": oid(99), "slack_id": "U1"})
    elif change == "malformed":
        source.data["slack_users"][0]["slack_id"] = "U1\n"
    else:
        source.data["members"][0]["merged_at"] = now()
    identities = source.identities([oid(1), oid(2)])
    assert sid(oid(1)) not in identities
    assert identities[sid(oid(2))]["slack_id"] == "U2"
    assert source.identity("U1") is None
    if change != "merged":
        assert source.slack_ids([oid(1), oid(2)]) == {sid(oid(2)): "U2"}
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "false")
    assert source.identities([oid(1), oid(2)]) == identities


def test_invalidated_duplicate_does_not_invalidate_live_identity(env):
    source = env[2]
    source.data["slack_users"].append({"_id": oid(800), "member_id": oid(99), "slack_id": "U1", "invalidated_at": now()})
    assert source.slack_ids([oid(1)]) == {sid(oid(1)): "U1"}
    assert source.identity("U1")["_id"] == oid(1)


def test_mongo_identity_batches_are_single_aggregations():
    database = MagicMock()
    collection = database.__getitem__.return_value
    source = Sources(database)
    collection.aggregate.return_value = [{"_id": oid(1), "slack_id": "U1", "status": "activeMember"}]
    assert source.identities([oid(1), oid(2)])[sid(oid(1))]["slack_id"] == "U1"
    assert collection.aggregate.call_count == 1
    pipeline = collection.aggregate.call_args.args[0]
    assert sum("$lookup" in stage for stage in pipeline) == 2
    assert sum(stage.get("$match", {}).get("_reverse.1") == {"$exists": False} for stage in pipeline) == 1
    source.slack_ids([oid(1), oid(2)])
    assert collection.aggregate.call_count == 2
    collection.find.assert_not_called()


def test_identity_pipeline_discards_unrelated_fields_before_grouping():
    pipeline = identity_pipeline("U1", "status")
    assert pipeline[:3] == [{"$match": {"slack_id": "U1", "invalidated_at": None}},
                           {"$project": {"_id": 1, "member_id": 1, "slack_id": 1}}, {"$limit": 2}]
    assert pipeline[3]["$group"]["link"] == {"$first": "$$ROOT"}


def test_graph_pipeline_discards_unrelated_ancestor_fields_before_grouping():
    pipeline = skill_graph_pipeline(oid(1))
    index = next(i for i, stage in enumerate(pipeline) if "$graphLookup" in stage)
    node_arrays = pipeline[index + 1]["$project"]["nodes"]["$concatArrays"]
    ancestor_map = node_arrays[0]["$map"]
    expected = {field: "$$node." + field for field in ("_id", *SKILL_TOOL_FIELDS.split())}
    assert ancestor_map == {"input": "$ancestors", "as": "node", "in": {**expected, "_cleared": False}}
    # Missing prerequisite_ids stays missing instead of becoming null: the
    # existing Python solver treats a missing list as an independent root.
    assert ancestor_map["in"]["prerequisite_ids"] == "$$node.prerequisite_ids"
    assert pipeline[index + 2] == {"$unwind": "$nodes"}
    assert pipeline[index + 3] == {"$replaceWith": "$nodes"}
    assert pipeline[index + 4]["$group"]["node"] == {"$first": "$$ROOT"}


def _graph_fixture(source):
    source.data["tools"] = [
        {"_id": oid(1000), "name": "Root", "shop_id": oid(201), "prerequisite_ids": []},
        {"_id": oid(1001), "name": "Middle", "shop_id": oid(201), "prerequisite_ids": [oid(1000)]},
        {"_id": oid(1002), "name": "Diamond", "shop_id": oid(201), "prerequisite_ids": [oid(1000), oid(1001)]},
        {"_id": oid(1003), "name": "Cycle A", "shop_id": oid(201), "prerequisite_ids": [oid(1004)]},
        {"_id": oid(1004), "name": "Cycle B", "shop_id": oid(201), "prerequisite_ids": [oid(1003)]},
        {"_id": oid(1005), "name": "Dangling", "shop_id": oid(201), "prerequisite_ids": [oid(9999)]},
        {"_id": oid(1006), "name": "Unrelated", "shop_id": oid(202), "prerequisite_ids": []},
    ]
    source.data["tool_checkouts"] = [{"_id": oid(1100 + i), "member_id": oid(1), "tool_id": oid(1000 + i)} for i in (2, 3, 5)]


def _standalone_graph_source(style="native"):
    source = MemorySources({"shops": [{"_id": oid(201), "name": "Graph shop"},
                                      {"_id": oid(202), "name": "Other shop"}]})
    _graph_fixture(source)
    source.data["tools"].append({"_id": oid(1007), "name": "Deepest", "shop_id": oid(201),
                                 "prerequisite_ids": [oid(1002)]})
    source.data["tool_checkouts"][0]["tool_id"] = oid(1007)
    for index, tool in enumerate(source.data["tools"]):
        tool["internal_notes"] = "SECRET graph payload"
        if style == "strings":
            tool["prerequisite_ids"] = [sid(value) for value in tool["prerequisite_ids"]]
        elif style == "mixed":
            tool["prerequisite_ids"] = [sid(value) if (index + edge) % 2 else value
                                         for edge, value in enumerate(tool["prerequisite_ids"])]
    return source


@pytest.mark.parametrize("style,calls", [("native", 1), ("strings", 3), ("mixed", 3)])
def test_graph_mixed_ids_use_batched_frontiers_standalone(style, calls, monkeypatch):
    source = _standalone_graph_source(style)
    with patch.object(source, "_skill_graph_rows", wraps=source._skill_graph_rows) as reads:
        graph = source.skill_graph(oid(1))
    assert reads.call_count == calls
    assert reads.call_args_list[0].kwargs["member_id"] == oid(1)
    if calls > 1:
        # A whole unresolved level is fetched in one command, including the
        # diamond/cycle paths; failed IDs are never retried in later levels.
        first = set(reads.call_args_list[1].kwargs["tool_ids"])
        assert oid(1002) in first and oid(1004) in first
        assert all(not isinstance(value, str) for call in reads.call_args_list[1:]
                   for value in call.kwargs["tool_ids"])
        requested = [value for call in reads.call_args_list[1:] for value in call.kwargs["tool_ids"]]
        assert len(requested) == len(set(requested))
    assert sid(oid(1006)) not in graph["tools"]
    assert "SECRET" not in str(graph)
    result = highest_skill(source, oid(1))
    assert result == {"highest_skill": "Deepest", "highest_skill_shop": "Graph shop", "highest_skill_depth": 3}
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "false")
    assert highest_skill(source, oid(1)) == result


@pytest.mark.parametrize("change", ["disabled", "disabled_parent", "revoked", "cycle", "dangling"])
def test_mixed_graph_invalid_paths_remain_excluded_standalone(change, monkeypatch):
    source = _standalone_graph_source("mixed")
    if change == "disabled":
        source.data["tools"][1]["disabled"] = True
    elif change == "disabled_parent":
        source.data["shops"][0]["disabled"] = True
    elif change == "revoked":
        source.data["tool_checkouts"][0]["revoked_at"] = now()
    elif change == "cycle":
        source.data["tools"][0]["prerequisite_ids"] = [sid(oid(1007))]
    else:
        source.data["tools"][0]["prerequisite_ids"] = [sid(oid(9998))]
    result = highest_skill(source, oid(1))
    assert result == {}
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "false")
    assert highest_skill(source, oid(1)) == result


def test_mongo_graph_frontiers_remain_fixed_narrow_aggregations_standalone():
    memory = _standalone_graph_source("strings")
    source = Sources(MagicMock())
    def aggregate(collection, pipeline, timeout_ms=2000):
        if collection == "tool_checkouts":
            return memory._skill_graph_rows(member_id=oid(1), timeout_ms=timeout_ms)
        assert pipeline == skill_ancestor_pipeline(pipeline[0]["$match"]["_id"]["$in"])
        return memory._skill_graph_rows(tool_ids=pipeline[0]["$match"]["_id"]["$in"], timeout_ms=timeout_ms)
    with patch.object(source, "_aggregate", side_effect=aggregate) as reads:
        graph = source.skill_graph(oid(1))
    assert graph == memory.skill_graph(oid(1))
    assert [call.args[0] for call in reads.call_args_list] == ["tool_checkouts", "tools", "tools"]
    assert all(0 < call.args[2] <= 2000 for call in reads.call_args_list)
    pipeline = reads.call_args_list[1].args[1]
    assert pipeline[1] == {"$project": {"_id": 1, **dict.fromkeys(SKILL_TOOL_FIELDS.split(), 1)}}
    index = next(i for i, stage in enumerate(pipeline) if "$graphLookup" in stage)
    seed = pipeline[index + 1]["$project"]["nodes"]["$concatArrays"][1]
    assert seed == [{"$mergeObjects": ["$tool", {"_cleared": False}]}]


def test_graph_frontiers_share_one_overall_deadline_standalone():
    source = Sources(MagicMock())
    memory = _standalone_graph_source("strings")
    # The first source response consumed the entire budget; no frontier read is
    # allowed to restart a fresh two-second timeout or return a partial graph.
    rows = memory._skill_graph_rows(member_id=oid(1))
    with patch("ledger.sources.time.monotonic", side_effect=[0, 0, 2.1]), \
            patch("ledger.sources.timeout", return_value=nullcontext()), \
            patch.object(source, "_aggregate", return_value=rows) as reads:
        with pytest.raises(TimeoutError, match="Skill graph read deadline exceeded"):
            source.skill_graph(oid(1))
    assert reads.call_count == 1


@pytest.mark.parametrize("change", ["none", "open", "disabled", "disabled_parent", "revoked", "out_of_service"])
def test_live_graph_preserves_longest_depth_and_invalid_paths(env, monkeypatch, change):
    source = env[2]
    _graph_fixture(source)
    if change == "open":
        source.data["tools"][2]["open"] = True
    elif change == "disabled":
        source.data["tools"][1]["disabled"] = True
    elif change == "disabled_parent":
        source.data["shops"][0]["disabled"] = True
    elif change == "revoked":
        source.data["tool_checkouts"][0]["revoked_at"] = now()
    elif change == "out_of_service":
        source.data["tools"][2]["out_of_service"] = True
    optimized_result = highest_skill(source, sid(oid(1)))
    if change in ("none", "out_of_service"):
        assert optimized_result["highest_skill"] == "Diamond" and optimized_result["highest_skill_depth"] == 2
    else:
        assert optimized_result == {}
    graph = source.skill_graph(oid(1))
    assert sid(oid(1006)) not in graph["tools"]
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "false")
    assert highest_skill(source, sid(oid(1))) == optimized_result


def test_skill_summary_batches_shops_and_shared_prerequisites(joined, monkeypatch):
    ledger, _, source, *_ = joined
    source.data["tool_checkouts"].append({"_id": oid(700), "member_id": oid(1), "tool_id": oid(311)})
    with patch.object(source, "tool", side_effect=AssertionError("per-edge query")), patch.object(source, "shop", side_effect=AssertionError("per-shop query")), patch.object(source, "_projected_rows", wraps=source._projected_rows) as reads:
        result = skill_summary(ledger, sid(oid(1)))
    assert len(result["nodes"]) == 20
    assert reads.call_count == 3  # shops, all selected-shop tools, caller clearances
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "false")
    assert skill_summary(ledger, sid(oid(1))) == result


def test_cached_skill_labels_cannot_preserve_stale_safety(joined):
    ledger, _, source, *_ = joined
    refresh(ledger)
    source.data["tools"][0]["name"] = "Renamed live label"
    source.data["tools"][0]["out_of_service"] = True
    source.data["tools"][1]["disabled"] = True
    source.data["tools"][2]["prerequisite_ids"] = [oid(322)]
    source.data["tools"][3]["shop_id"] = oid(202)
    source.data["tool_checkouts"].append({"_id": oid(700), "member_id": oid(1), "tool_id": oid(311)})
    result = skill_summary(ledger, sid(oid(1)), "Shop1")
    assert [node["name"] for node in result["nodes"]] == ["Tool1-1", "Tool1-3"]
    assert result["nodes"][0]["state"] == "Cleared" and "temporarily unavailable" in result["text"]
    assert result["nodes"][1]["state"] == "Prerequisites remain"
    assert result["nodes"][1]["prerequisites"] == [sid(oid(322))]
    source.data["shops"][0]["disabled"] = True
    assert skill_summary(ledger, sid(oid(1)), "Shop1")["nodes"] == []


def test_progress_coverage_skips_irrelevant_earned_and_group_reads(env):
    source = env[2]
    with patch.object(source, "bounded", wraps=source.bounded) as reads, patch.object(source, "_projected_rows", side_effect=AssertionError("irrelevant group read")):
        assert source.eligible_for_rank(oid(1))
    assert [call.args[0] for call in reads.call_args_list] == ["members"]


def _standalone_skill_ledger():
    source = MemorySources({
        "shops": [{"_id": oid(201), "name": "Visible shop"}, {"_id": oid(202), "name": "Other shop"}],
        "tools": [
            {"_id": oid(1000), "name": "Cached descriptive label", "shop_id": oid(201), "prerequisite_ids": []},
            {"_id": oid(1001), "name": "Next tool", "shop_id": oid(201), "prerequisite_ids": [oid(1000)]},
            {"_id": oid(1002), "name": "New prerequisite", "shop_id": oid(202), "prerequisite_ids": []}],
        "tool_checkouts": [{"_id": oid(1100), "member_id": oid(1), "tool_id": oid(1000)}],
    })
    return SimpleNamespace(store=MemoryStore(), sources=source)


@pytest.mark.parametrize("stage", ["tools", "shops", "clearances", "prerequisites"])
@pytest.mark.parametrize("error_type", [OperationFailure, OSError, TimeoutError])
def test_cached_skill_safety_failure_is_unavailable_standalone(stage, error_type):
    ledger = _standalone_skill_ledger()
    source = ledger.sources
    refresh(ledger)
    assert display_catalog(ledger, "Visible shop")["tools"][0]["name"] == "Cached descriptive label"
    if stage == "prerequisites":
        source.data["tools"][1]["prerequisite_ids"] = [oid(1002)]
    method = "shops_by_id" if stage == "shops" else "clearances" if stage == "clearances" else "tools_by_id"
    original = getattr(source, method)
    def failed(*args, **kwargs):
        if stage != "prerequisites" or (len(args) > 1 and args[1] == ["name"]):
            raise error_type("SECRET source URI and internal exception detail")
        return original(*args, **kwargs)
    with patch.object(source, method, side_effect=failed) as live_read, \
            patch.object(source, "skill_catalog", side_effect=AssertionError("No fallback after failed safety read")) as fallback:
        result = skill_summary(ledger, sid(oid(1)), "Visible shop")
    assert result == {"text": "Skill paths are temporarily unavailable. Please try again.",
                      "nodes": [], "status": "unavailable"}
    assert "SECRET" not in str(result) and "Cached descriptive label" not in str(result)
    assert live_read.called
    fallback.assert_not_called()


def test_skill_success_and_disabled_switch_preserve_shape_standalone(monkeypatch):
    ledger = _standalone_skill_ledger()
    refresh(ledger)
    with patch("ledger.skills.timeout", wraps=timeout) as budget:
        result = skill_summary(ledger, sid(oid(1)), "Visible shop")
    budget.assert_called_once_with(2)
    assert set(result) == {"text", "nodes"}
    assert [node["state"] for node in result["nodes"]] == ["Cleared", "Next step"]
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "false")
    with patch("ledger.catalog_cache.display_catalog", side_effect=AssertionError("Disabled cache read")):
        assert skill_summary(ledger, sid(oid(1)), "Visible shop") == result


def test_uncached_skill_source_failure_is_unavailable_standalone():
    ledger = _standalone_skill_ledger()
    with patch.object(ledger.sources, "skill_catalog", side_effect=OperationFailure("SECRET source error")):
        result = skill_summary(ledger, sid(oid(1)))
    assert result["status"] == "unavailable" and result["nodes"] == []
    assert "SECRET" not in result["text"]


def _explain_returned(database, collection, pipeline):
    """Read the final output count, not a cursor's intermediate input count."""
    try:
        plan = database.command({"explain": {"aggregate": collection, "pipeline": pipeline,
                                "cursor": {}, "maxTimeMS": 2000, "allowDiskUse": False},
                                 "verbosity": "executionStats"})
    except OperationFailure as exc:
        message = str(exc).casefold()
        if exc.code in (59, 115) or (
                ("explain" in message or "executionstats" in message) and
                ("not supported" in message or "not allowed" in message)):
            return None
        raise
    stages = plan.get("stages")
    if stages:
        final = stages[-1]
        count = final.get("nReturned", final.get("$cursor", {}).get("executionStats", {}).get("nReturned"))
    else:
        count = plan.get("executionStats", {}).get("nReturned")
    assert isinstance(count, int) and not isinstance(count, bool), "executionStats did not expose a final nReturned"
    return count


def test_explain_accepts_bson_int64_return_count():
    database = MagicMock()
    database.command.return_value = {"executionStats": {"nReturned": Int64(3)}}
    assert _explain_returned(database, "tools", []) == 3


@pytest.mark.parametrize("plan,expected", [
    ({"stages": [{"$cursor": {"executionStats": {"nReturned": 2000}}},
                 {"$limit": 3, "nReturned": 3}]}, 3),
    ({"stages": [{"$cursor": {"executionStats": {"nReturned": 3}}}]}, 3),
    ({"executionStats": {"nReturned": 3}}, 3),
])
def test_explain_output_count_uses_final_stage(plan, expected):
    database = MagicMock()
    database.command.return_value = plan
    assert _explain_returned(database, "tools", catalog_pipeline("tools", limit=3)) == expected
    assert database.command.call_args.args[0]["verbosity"] == "executionStats"


@pytest.mark.skipif(not os.environ.get("LEDGER_TEST_MONGO_URI"), reason="Set LEDGER_TEST_MONGO_URI to a disposable Mongo replica set")
@pytest.mark.parametrize("style,commands", [("native", 1), ("strings", 3), ("mixed", 3)])
def test_real_source_pipelines_match_memory_and_legacy(monkeypatch, style, commands):
    source = _standalone_graph_source(style)
    source.data["members"] = [{"_id": oid(1), "status": "activeMember", "internal_notes": "SECRET"},
                              {"_id": oid(2), "status": "pending"}]
    source.data["slack_users"] = [{"_id": oid(500), "member_id": oid(1), "slack_id": "U1", "internal_notes": "SECRET"},
                                 {"_id": oid(501), "member_id": oid(2), "slack_id": "U2"},
                                 {"_id": oid(800), "member_id": oid(99), "slack_id": "U2"}]
    for tool in source.data["tools"]:
        tool["internal_notes"] = "SECRET" * 1000
    # Exercise omission of missing optional fields inside the ancestor map.
    source.data["tools"][0].pop("prerequisite_ids")
    client = MongoClient(os.environ["LEDGER_TEST_MONGO_URI"], serverSelectionTimeoutMS=2000, tz_aware=True)
    database_name = "ledger_test_source_reads_" + uuid4().hex
    database = client[database_name]
    live = Sources(database)
    try:
        for name, rows in source.data.items():
            if rows:
                database[name].insert_many(deepcopy(rows))
        for name in CATALOG_FIELDS:
            assert live.catalog_query(name, member_id=oid(1), boundary=now(), limit=3) == source.catalog_query(name, member_id=oid(1), boundary=now(), limit=3)
        assert live.slack_ids([oid(1), oid(2)]) == source.slack_ids([oid(1), oid(2)])
        assert live.identities([oid(1), oid(2)]) == source.identities([oid(1), oid(2)])
        assert live.identity("U1") == source.identity("U1")
        with patch.object(live, "_aggregate", wraps=live._aggregate) as graph_reads:
            graph = live.skill_graph(oid(1))
        assert graph_reads.call_count == commands
        assert graph == source.skill_graph(oid(1))
        assert "SECRET" not in str(graph)
        assert highest_skill(live, oid(1)) == highest_skill(source, oid(1))
        for collection in ("tools", "tool_checkouts"):
            returned = _explain_returned(database, collection,
                catalog_pipeline(collection, member_id=oid(1), boundary=now(), limit=3))
            if returned is not None:
                assert returned <= 3
        for call in graph_reads.call_args_list:
            collection, pipeline = call.args[:2]
            expected = source._skill_graph_rows(member_id=oid(1)) if collection == "tool_checkouts" else \
                       source._skill_graph_rows(tool_ids=pipeline[0]["$match"]["_id"]["$in"])
            returned = _explain_returned(database, collection, pipeline)
            if returned is not None:
                assert returned == len(expected)
                assert returned <= len(graph["tools"]) < len(source.data["tools"])
        # Unchanged source indexes can still examine many documents. These
        # executionStats checks guarantee bounded output, not scan counts.
        monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "false")
        assert highest_skill(live, oid(1))["highest_skill_depth"] == 3
    finally:
        client.drop_database(database_name)
        client.close()
