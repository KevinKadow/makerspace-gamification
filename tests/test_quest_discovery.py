"""Parity and query-shape checks for bounded, live-authorized quest discovery."""
from copy import deepcopy
import os
from uuid import uuid4

import pytest

from conftest import oid
from ledger.domain import Ledger
from ledger.quest_discovery import QuestDiscovery, backfill_quest_heads, quest_head
from ledger.quests import Quests
from ledger.storage import connect


def member(n):
    return str(oid(n))


def install(env, key="available", title="Available build", **changes):
    ledger, store, *_ = env
    if not ledger.participant(member(3)):
        ledger.join(member(3))
    author = ledger.participant(member(3))
    author["rank"] = 4
    store.put("ledger_participants", author)
    q = {"_id": key, "logical_id": key, "kind": "member_quest", "creator": member(3),
         "title": title, "description": "A safe build", "criteria": "Observable results", "target_rank": 1,
         "shop_ids": [], "tool_ids": [], "disciplines": [], "status": "published", "reward": 100,
         "classification": "challenge", **changes}
    store.put("ledger_quests", q)
    store.put("ledger_catalog", quest_head(q))
    return q


def test_publication_metadata_backfill_is_idempotent_and_preserves_revision(joined):
    _, store, *_ = joined
    q = install(joined, title="Straße [literal]")
    store.put("ledger_catalog", {"_id": "quest-head:" + q["logical_id"], "revision": q["_id"]})
    acceptance = {"_id": f"acceptance:{member(1)}:{q['logical_id']}", "kind": "quest_acceptance",
                  "member_id": member(1), "quest_revision": q["_id"], "logical_id": q["logical_id"]}
    store.put("ledger_relationships", acceptance)
    for row in store.data["ledger_catalog"].values():
        if row.get("kind") == "challenge":
            row.pop("title_key", None)  # A pre-preparation deployment's records.
    before = deepcopy(store.get("ledger_quests", q["_id"]))
    assert backfill_quest_heads(store) == {"heads": 1, "acceptances": 1, "display_titles": 3}
    assert store.get("ledger_catalog", "quest-head:" + q["logical_id"]) == quest_head(q)
    assert store.get("ledger_relationships", acceptance["_id"])["title_key"] == "strasse [literal]"
    assert backfill_quest_heads(store) == {"heads": 0, "acceptances": 0, "display_titles": 0}
    assert store.get("ledger_quests", q["_id"]) == before


def test_acceptance_wins_after_rank_change_and_new_revision(joined):
    ledger, store, *_ = joined
    original = install(joined, title="Old build")
    Quests(ledger).accept(member(1), original["_id"])
    install(joined, key="replacement", title="New build", logical_id=original["logical_id"])
    caller = ledger.participant(member(1))
    caller["rank"] = 2
    store.put("ledger_participants", caller)
    result = QuestDiscovery(ledger, member(1)).listing()
    assert [q["_id"] for q in result] == [original["_id"]]
    original["status"] = "withdrawn"
    store.put("ledger_quests", original)
    assert QuestDiscovery(ledger, member(1)).listing() == []


def test_page_fills_after_rejected_candidates_without_per_candidate_queries(joined, monkeypatch):
    ledger, store, source, *_ = joined
    for i in range(40):
        install(joined, key=f"unavailable-{i:03}", title=f"A unavailable {i:03}", creator=member(1))
    desired = install(joined, title="Z available", tool_ids=[str(oid(311))])
    source.data["tool_checkouts"] = [{"_id": oid(900), "member_id": oid(1), "tool_id": oid(311)}]
    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery must batch authority/prerequisite reads")
    monkeypatch.setattr(Quests, "eligible", forbidden)
    monkeypatch.setattr(Quests, "author_available", forbidden)
    monkeypatch.setattr(Quests, "prerequisites", forbidden)
    monkeypatch.setattr(source, "tool", forbidden)
    monkeypatch.setattr(source, "shop", forbidden)
    pages = []
    original_select = store.select
    def select(collection, query=None, **kwargs):
        result = original_select(collection, query, **kwargs)
        if collection == "ledger_catalog" and kwargs.get("limit"):
            pages.append(len(result))
            assert kwargs["limit"] == 32
        return result
    monkeypatch.setattr(store, "select", select)
    assert QuestDiscovery(ledger, member(1)).listing()[0]["_id"] == desired["_id"]
    assert pages[:2] == [32, 9]


def test_live_clearance_disabled_parent_and_ambiguous_author_hide_results(joined):
    ledger, _, source, *_ = joined
    q = install(joined, tool_ids=[str(oid(311))])
    source.data["tool_checkouts"] = [{"_id": oid(900), "member_id": oid(1), "tool_id": oid(311)}]
    assert QuestDiscovery(ledger, member(1)).listing()[0]["_id"] == q["_id"]
    source.data["tool_checkouts"][0]["revoked_at"] = "revoked"
    assert QuestDiscovery(ledger, member(1)).listing() == []
    source.data["tool_checkouts"][0].pop("revoked_at")
    source.data["shops"][0]["disabled"] = True
    assert QuestDiscovery(ledger, member(1)).listing() == []
    source.data["shops"][0].pop("disabled")
    source.data["slack_users"].append({"_id": oid(999), "member_id": oid(4), "slack_id": "U3"})
    assert QuestDiscovery(ledger, member(1)).listing() == []


def test_literal_casefold_search_and_limits_match_legacy(joined, monkeypatch):
    ledger, *_ = joined
    for i in range(110):
        install(joined, key=f"build-{i:03}", title=f"Build {i:03}")
    selected = install(joined, title="Straße [literal]")
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "false")
    old = Quests(ledger).options(member(1), "[literal]")
    monkeypatch.setenv("LEDGER_OPTIMIZED_READS", "true")
    assert Quests(ledger).options(member(1), "[literal]") == old == [("q:" + selected["_id"], selected["title"])]
    assert Quests(ledger).options(member(1), "STRASSE") == old
    options = Quests(ledger).options(member(1), "Build ")
    assert len(options) == 100
    assert options[0][1] == "Build 000" and options[-1][1] == "Build 099"


def test_opted_out_author_remains_valid_and_completed_quest_disappears(joined):
    ledger, store, *_ = joined
    q = install(joined)
    ledger.leave(member(3))
    assert QuestDiscovery(ledger, member(1)).listing()[0]["_id"] == q["_id"]
    store.put("ledger_evidence", {"_id": f"quest-complete:{member(1)}:{q['logical_id']}",
                                 "kind": "quest_completion", "member_id": member(1), "quest_revision": q["_id"]})
    assert QuestDiscovery(ledger, member(1)).listing() == []


def test_duplicate_titles_use_revision_order_before_limit(joined):
    ledger, *_ = joined
    for i in range(105):
        install(joined, key=f"revision-{i:03}", title="Same title", logical_id=f"logical-{104-i:03}")
    rows = QuestDiscovery(ledger, member(1)).listing()
    assert [row["_id"] for row in rows] == [f"revision-{i:03}" for i in range(100)]


def test_challenge_pages_stop_after_first_hundred_ordered_matches(joined, monkeypatch):
    ledger, store, *_ = joined
    for i in range(300):
        store.put("ledger_catalog", {"_id": f"challenge-{299-i:03}", "kind": "challenge", "active": True,
                                    "title": f"Ordered challenge {i:03}", "title_key": f"ordered challenge {i:03}"})
    page_lengths = []
    original_select = store.select
    def select(collection, query=None, **kwargs):
        result = original_select(collection, query, **kwargs)
        if collection == "ledger_catalog" and kwargs.get("sort") == [("title_key", 1), ("_id", 1)]:
            page_lengths.append(len(result))
        return result
    monkeypatch.setattr(store, "select", select)
    result = QuestDiscovery(ledger, member(1)).options("Ordered challenge")
    assert [title for _, title in result] == [f"Ordered challenge {i:03}" for i in range(100)]
    assert page_lengths == [32, 32, 32, 32]


def test_discovery_uses_one_total_two_second_database_budget(joined, monkeypatch):
    from contextlib import contextmanager
    import ledger.quest_discovery as discovery
    ledger, *_ = joined
    install(joined)
    calls, active = [], False
    @contextmanager
    def budget(seconds):
        nonlocal active
        assert not active, "Nested paging must share one request budget"
        active = True
        calls.append(seconds)
        try:
            yield
        finally:
            active = False
    original_require = ledger.require
    def require(*args):
        assert active, "Caller authorization belongs inside the request deadline"
        return original_require(*args)
    monkeypatch.setattr(discovery, "timeout", budget)
    monkeypatch.setattr(ledger, "require", require)
    assert QuestDiscovery(ledger, member(1)).options()
    assert calls == [2]


@pytest.mark.skipif(not os.environ.get("LEDGER_TEST_MONGO_URI"),
                    reason="Set LEDGER_TEST_MONGO_URI to a disposable Mongo replica set")
def test_real_owned_quest_joins_and_keyset_pages(joined):
    _, memory, source, *_ = joined
    for i in range(35):
        install(joined, key=f"old-{i:03}", title=f"A old {i:03}", status="withdrawn")
    wanted = install(joined, title="Z visible")
    database = "ledger_test_" + uuid4().hex
    store = connect(os.environ["LEDGER_TEST_MONGO_URI"], database)
    try:
        store.ready()
        store.indexes()
        for collection, documents in memory.data.items():
            if documents:
                store.db[collection].insert_many(list(documents.values()))
        result = QuestDiscovery(Ledger(store, source), member(1)).listing()
        assert [q["_id"] for q in result] == [wanted["_id"]]
    finally:
        store.db.client.drop_database(database)
