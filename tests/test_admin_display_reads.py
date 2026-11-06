"""Admin queue pagination keeps scope and independence ahead of display limits."""
from copy import deepcopy

import pytest

from conftest import oid
from ledger.authority import Authority
from ledger.display_reads import AdminDisplay, DisplaySources, DisplayStore
from ledger.domain import Denied
from ledger.quest_policy import LEDGER_AUTHOR
from ledger.worker import Worker


def member(n):
    return str(oid(n))


def scoped_reviewer(env, capabilities=("learning_review",)):
    ledger, _, source, *_ = env
    source.data["members"][9].update(role="resource_manager", resource_manager_shop_ids=[oid(201)])
    return Authority(ledger).grant(member(10), member(2), list(capabilities),
                                  {"kind": "shops", "shops": [member(201)]}, "Independent scoped review")


def evidence(store, identifier, subject=1, shop=201, **fields):
    doc = {"_id": identifier, "kind": "submission", "status": "pending", "member_id": member(subject),
           "shop_id": member(shop), "shop_ids": [member(shop)], "achievement": "challenge",
           "description": "Observable scoped evidence", **fields}
    store.put("ledger_evidence", doc)
    return doc


def test_scoped_queue_fills_beyond_thirty_two_self_excluded_candidates(joined, monkeypatch):
    ledger, store, source, *_ = joined
    scoped_reviewer(joined)
    for i in range(40):
        evidence(store, f"a-self-{i:03}", subject=2)
        evidence(store, f"a-outside-{i:03}", shop=202, description="UNAUTHORIZED_PRIVATE_BODY")
    evidence(store, "z-visible")
    page_lengths, identity_batches = [], []
    original_select, original_identities = store.select, source.identities
    def select(collection, query=None, **kwargs):
        result = original_select(collection, query, **kwargs)
        if collection == "ledger_evidence" and kwargs.get("limit"):
            assert kwargs["limit"] == 32 and kwargs["max_time_ms"] == 2000
            assert kwargs["projection"]
            if result:
                assert all(row.get("shop_id") != member(202) for row in result)
                page_lengths.append(len(result))
        return result
    def identities(ids):
        values = set(ids)
        if values:
            identity_batches.append(values)
        return original_identities(values)
    monkeypatch.setattr(store, "select", select)
    monkeypatch.setattr(source, "identities", identities)
    result = AdminDisplay(ledger, member(2)).pending_reviews()
    assert "z-visible: challenge" in result
    assert "a-self-" not in result and "UNAUTHORIZED_PRIVATE_BODY" not in result
    assert page_lengths == [32, 9]
    flattened = [m for batch in identity_batches for m in batch]
    assert len(flattened) == len(set(flattened)), "Batch live identities once per display request"


def test_review_grant_snapshot_never_replaces_fresh_commit_authority(joined):
    ledger, store, source, *_ = joined
    grant = scoped_reviewer(joined)
    doc = ledger.submit(member(1), "learning-challenge", "Scoped review evidence", shop=member(201), key="fresh")
    displayed = AdminDisplay(ledger, member(2))
    assert doc["_id"] in displayed.pending_reviews()
    Authority(ledger).revoke(member(10), grant["_id"], "Revoke before approval")
    assert doc["_id"] not in AdminDisplay(ledger, member(2)).pending_reviews()
    with pytest.raises(Denied):
        ledger.review(member(2), doc["_id"])
    assert store.get("ledger_evidence", doc["_id"])["status"] == "pending"
    assert not store.select("ledger_awards", {"kind": "review"})
    # The earlier display is a request snapshot; a source permission change must
    # also be denied by the mutation path rather than trusting cached display.
    source.data["members"][9]["resource_manager_shop_ids"] = []
    assert doc["_id"] not in AdminDisplay(ledger, member(2)).pending_reviews()


def test_multishop_and_author_exclusions_are_rechecked_after_database_scope_filter(joined):
    ledger, store, *_ = joined
    scoped_reviewer(joined, ("learning_review", "quest_publish", "quest_complete"))
    evidence(store, "multishop", shop_ids=[member(201), member(202)])
    evidence(store, "valid-single")
    creator = {"_id": "author-revision", "kind": "member_quest", "creator": member(2),
               "logical_id": "author-logical", "shop_ids": [member(201)], "title": "Own proposal", "status": "pending_review"}
    store.put("ledger_quests", creator)
    store.put("ledger_evidence", {"_id": "author-completion", "kind": "quest_submission", "status": "pending",
        "quest_revision": creator["_id"], "member_id": member(1), "description": "Author cannot independently review"})
    rendered = AdminDisplay(ledger, member(2)).pending_reviews()
    assert "valid-single" in rendered
    assert "multishop" not in rendered
    assert "author-revision" not in rendered and "author-completion" not in rendered


def test_invalid_delegate_identity_and_lost_grantor_scope_remove_reviews(joined):
    ledger, store, source, *_ = joined
    scoped_reviewer(joined)
    evidence(store, "scoped-item")
    assert "scoped-item" in AdminDisplay(ledger, member(2)).pending_reviews()
    source.data["slack_users"].append({"_id": oid(999), "member_id": oid(4), "slack_id": "U2"})
    assert "scoped-item" not in AdminDisplay(ledger, member(2)).pending_reviews()
    source.data["slack_users"].pop()
    source.data["members"][9]["resource_manager_shop_ids"] = []
    assert "scoped-item" not in AdminDisplay(ledger, member(2)).pending_reviews()


def test_display_caches_return_independent_copies_and_missing_people_once(joined, monkeypatch):
    ledger, store, source, *_ = joined
    cached_store = DisplayStore(store)
    cached_store.warm("ledger_participants", [member(1)])
    copy = cached_store.get("ledger_participants", member(1))
    copy["rank"] = 99
    assert cached_store.get("ledger_participants", member(1))["rank"] == 1
    source_calls = []
    original = source.identities
    def identities(ids):
        ids = set(ids)
        if ids:
            source_calls.append(ids)
        return original(ids)
    monkeypatch.setattr(source, "identities", identities)
    cached_source = DisplaySources(source)
    cached_source.warm_people([member(1), "missing-person"])
    before = deepcopy(cached_source.member(member(1)))
    cached_source.member(member(1))["status"] = "suspended"
    assert cached_source.member(member(1)) == before
    assert cached_source.member("missing-person") is None
    assert source_calls == [{member(1), "missing-person"}]


def test_scoped_completion_join_filters_bodies_before_paging_and_fills_after_independence(joined, monkeypatch):
    ledger, store, *_ = joined
    scoped_reviewer(joined, ("quest_complete",))
    for key, shop in (("inside-revision", 201), ("outside-revision", 202)):
        store.put("ledger_quests", {"_id": key, "kind": "member_quest", "creator": member(3),
            "logical_id": key, "shop_ids": [member(shop)], "title": key, "status": "published"})
    for i in range(40):
        store.put("ledger_evidence", {"_id": f"a-outside-{i:03}", "kind": "quest_submission", "status": "pending",
            "quest_revision": "outside-revision", "member_id": member(1), "description": "OUTSIDE_COMPLETION_PRIVATE_BODY"})
        store.put("ledger_evidence", {"_id": f"b-self-{i:03}", "kind": "quest_submission", "status": "pending",
            "quest_revision": "inside-revision", "member_id": member(2), "description": "Self review excluded"})
    store.put("ledger_evidence", {"_id": "z-valid", "kind": "quest_submission", "status": "pending",
        "quest_revision": "inside-revision", "member_id": member(1), "description": "Independent completion evidence"})
    pages = []
    original = store.aggregate
    def aggregate(collection, pipeline, **kwargs):
        result = original(collection, pipeline, **kwargs)
        if collection == "ledger_evidence":
            assert kwargs["max_time_ms"] == 2000
            lookup = next(i for i, step in enumerate(pipeline) if "$lookup" in step)
            limit = next(i for i, step in enumerate(pipeline) if "$limit" in step)
            assert any("$match" in step for step in pipeline[lookup + 1:limit])
            assert all(row["quest"]["shop_ids"] == [member(201)] for row in result)
            assert "OUTSIDE_COMPLETION_PRIVATE_BODY" not in str(result)
            pages.append(len(result))
        return result
    monkeypatch.setattr(store, "aggregate", aggregate)
    summary = AdminDisplay(ledger, member(2)).pending_reviews()
    assert "Quest completion: z-valid" in summary
    assert "b-self-" not in summary and "OUTSIDE_COMPLETION_PRIVATE_BODY" not in summary
    assert pages == [32, 9]


def cooperative_fixture(env, key, shop=201, actor_contributes=False, pending=True, malformed=None):
    _, store, *_ = env
    q = {"_id": key, "logical_id": key, "kind": "ledger_quest", "creator": LEDGER_AUTHOR,
         "quest_type": "cooperative", "target_rank": 1, "status": "published", "title": "Build " + key,
         "description": "Build safely together", "criteria": "Show a working build", "shop_ids": [member(shop)],
         "tool_ids": [member(311 if shop == 201 else 321)],
         "disciplines": [{"name": "Design", "expectation": "Document dimensions"},
                         {"name": "Fabrication", "expectation": "Demonstrate construction"}], "reward": 100}
    contributions = {member(1): {"role": "Design", "status": "pending" if pending else "verified", "description": "Design evidence"},
                     member(2): {"role": "Fabrication", "status": "verified", "description": "Fabrication evidence"}}
    if actor_contributes:
        contributions[member(3)] = {"role": "Design", "status": "joined"}
    state = {"_id": "cooperative:" + key, "kind": "quest_project", "logical_id": key,
             "quest_revision": key, "status": "open", "contributions": malformed if malformed is not None else contributions}
    store.put("ledger_quests", q)
    store.put("ledger_relationships", state)
    return q, state


def cooperative_reviewer(env):
    ledger, _, source, *_ = env
    ledger.join(member(3))
    source.data["members"][9].update(role="resource_manager", resource_manager_shop_ids=[oid(201)])
    Authority(ledger).grant(member(10), member(3), ["quest_complete"],
                            {"kind": "shops", "shops": [member(201)]}, "Scoped shared review")
    source.data["tool_checkouts"] = [{"_id": oid(901), "member_id": oid(1), "tool_id": oid(311)},
                                    {"_id": oid(902), "member_id": oid(2), "tool_id": oid(311)}]


def test_cooperative_join_batches_live_safety_and_fills_authorized_page_after_denials(joined, monkeypatch):
    ledger, store, source, composer, _, slack = joined
    cooperative_reviewer(joined)
    for i in range(40):
        _, outside = cooperative_fixture(joined, f"a-outside-{i:03}", shop=202)
        outside["contributions"][member(1)]["description"] = "OUTSIDE_SHARED_PRIVATE_BODY"
        store.put("ledger_relationships", outside)
        cooperative_fixture(joined, f"b-self-{i:03}", actor_contributes=True)
    cooperative_fixture(joined, "z-visible")
    pages, batches, checkout_queries, tool_batches = [], [], [], []
    original_aggregate, original_identities = store.aggregate, source.identities
    original_rows, original_tools = source.rows, source.tools_by_id
    def aggregate(collection, pipeline, **kwargs):
        result = original_aggregate(collection, pipeline, **kwargs)
        if collection == "ledger_relationships":
            assert all(row["quest"]["shop_ids"] == [member(201)] for row in result)
            assert "OUTSIDE_SHARED_PRIVATE_BODY" not in str(result)
            pages.append(len(result))
        return result
    def identities(ids):
        values = set(ids)
        if values:
            batches.append(values)
        return original_identities(values)
    def rows(collection, query=None):
        if collection == "tool_checkouts":
            checkout_queries.append(deepcopy(query))
            assert isinstance(query["member_id"], dict), "Warm clearances once per page rather than per contributor"
        return original_rows(collection, query)
    def tools(ids, fields=None):
        values = set(ids)
        if values:
            tool_batches.append(values)
        return original_tools(values, fields)
    def forbidden(*args, **kwargs):
        raise AssertionError("Cooperative display must use batched identities and safety facts")
    monkeypatch.setattr(store, "aggregate", aggregate)
    monkeypatch.setattr(source, "identities", identities)
    monkeypatch.setattr(source, "rows", rows)
    monkeypatch.setattr(source, "tools_by_id", tools)
    monkeypatch.setattr(source, "member", forbidden)
    monkeypatch.setattr(source, "slack_id", forbidden)
    monkeypatch.setattr(source, "tool", forbidden)
    monkeypatch.setattr(source, "shop", forbidden)
    summary = Worker(ledger, composer, slack).cooperative_reviews(member(3))
    assert "Cooperative contribution: z-visible" in summary
    assert "b-self-" not in summary and "OUTSIDE_SHARED_PRIVATE_BODY" not in summary
    assert pages == [32, 9]
    assert len(checkout_queries) == 2 and tool_batches == [{member(311)}]
    flattened = [m for batch in batches for m in batch]
    assert len(flattened) == len(set(flattened))


@pytest.mark.parametrize("shared", [True, False])
@pytest.mark.parametrize("malformed", [42, [{"status": "pending"}]])
def test_malformed_contribution_shapes_do_not_suppress_valid_shared_reviews(joined, shared, malformed):
    ledger, store, _, composer, _, slack = joined
    cooperative_reviewer(joined)
    cooperative_fixture(joined, "z-valid")
    if shared:
        cooperative_fixture(joined, "a-malformed", malformed=malformed)
    else:
        store.put("ledger_quests", {"_id": "a-malformed", "status": "open", "title": "Malformed old quest",
            "creator": member(4), "shop_id": member(201), "contributions": malformed})
    summary = Worker(ledger, composer, slack).cooperative_reviews(member(3))
    assert "Cooperative contribution: z-valid" in summary
    assert "a-malformed" not in summary


def test_shared_ready_view_disappears_with_live_clearance_and_scope_loss(joined):
    ledger, _, source, composer, _, slack = joined
    cooperative_reviewer(joined)
    cooperative_fixture(joined, "ready", pending=False)
    worker = Worker(ledger, composer, slack)
    assert "Shared project ready: ready" in worker.cooperative_reviews(member(3))
    source.data["tool_checkouts"][0]["revoked_at"] = "revoked"
    assert "Shared project ready: ready" not in worker.cooperative_reviews(member(3))
    source.data["tool_checkouts"][0].pop("revoked_at")
    source.data["members"][9]["resource_manager_shop_ids"] = []
    assert "Shared project ready: ready" not in worker.cooperative_reviews(member(3))


def test_one_large_cooperative_parent_does_not_exceed_display_limit(joined):
    ledger, store, source, composer, _, slack = joined
    cooperative_reviewer(joined)
    contributions = {}
    for i in range(101):
        identifier = oid(1000 + i)
        source.data["members"].append({"_id": identifier, "status": "activeMember", "role": "member"})
        source.data["slack_users"].append({"_id": oid(2000 + i), "member_id": identifier, "slack_id": f"U{1000+i}"})
        contributions[str(identifier)] = {"status": "pending", "description": "Observable contribution"}
    store.put("ledger_quests", {"_id": "large-legacy-group", "status": "open", "creator": member(4),
        "title": "Many contributors", "shop_id": member(201), "contributions": contributions})
    result = Worker(ledger, composer, slack).cooperative_reviews(member(3))
    assert result.count("Cooperative contribution:") == 100


def test_legacy_open_quest_scoped_grant_uses_parent_id_without_logical_id(joined):
    ledger, store, source, composer, _, slack = joined
    ledger.join(member(3))
    source.data["members"][9].update(role="resource_manager", resource_manager_shop_ids=[oid(201)])
    for key in ("selected-legacy", "unselected-legacy"):
        store.put("ledger_quests", {"_id": key, "status": "open", "creator": member(4), "shop_id": member(201),
            "title": key, "contributions": {member(1): {"status": "pending", "description": "Observable shared work"}}})
    grant = Authority(ledger).grant(member(10), member(3), ["quest_complete"],
                                    {"kind": "quest", "quest": "selected-legacy"}, "One historical group quest")
    assert grant["scope"] == {"kind": "quest", "quest": "selected-legacy"}
    assert "logical_id" not in store.get("ledger_quests", "selected-legacy")
    rendered = Worker(ledger, composer, slack).cooperative_reviews(member(3))
    assert "Cooperative contribution: selected-legacy" in rendered
    assert "unselected-legacy" not in rendered
