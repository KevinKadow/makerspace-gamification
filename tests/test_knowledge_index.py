from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import time

import pytest

from ledger.knowledge_index import KnowledgeIndex, PrivateEmbeddings, _LiveHumanVerifier, _role_boost, thread_url
from ledger.storage import MemoryStore


def record(key="T1:C1:1700000000.000001", generation="v1", date=None, users=None):
    when = date or datetime.now(timezone.utc)
    return {"_id": key, "generation": generation, "indexed_generation": generation, "status": "ready", "channel_id": "C1",
            "channel_name": "woodshop", "thread_ts": "1700000000.000001", "date_start": when,
            "date_end": when, "participant_ids": users or [],
            "summary": {"topic": "Saw", "participants": ["Sam"], "problem_context": "Stopped",
                        "resolution_decision": "Replaced blade", "action_items": []}}


class Vectors:
    def __init__(self):
        self.documents = {}

    def add_texts(self, texts, metadatas, ids):
        for text, metadata, key in zip(texts, metadatas, ids):
            self.documents[key] = SimpleNamespace(page_content=text, metadata=metadata)

    def delete(self, where):
        if "$and" in where:
            thread = where["$and"][0]["thread_id"]
            generation = where["$and"][1]["generation"]
            self.documents = {k: v for k, v in self.documents.items()
                              if v.metadata["thread_id"] != thread or v.metadata["generation"] != generation}
        else:
            self.documents = {k: v for k, v in self.documents.items() if v.metadata["thread_id"] != where["thread_id"]}

    def similarity_search_with_score(self, question, **kwargs):
        def matches(metadata, condition):
            if "$and" in condition:
                return all(matches(metadata, clause) for clause in condition["$and"])
            key, expected = next(iter(condition.items()))
            actual = metadata.get(key)
            if isinstance(expected, dict):
                if "$gte" in expected:
                    return actual is not None and actual >= expected["$gte"]
                if "$lte" in expected:
                    return actual is not None and actual <= expected["$lte"]
            return actual == expected
        selected = [doc for doc in self.documents.values() if matches(doc.metadata, kwargs.get("filter", {}))] if kwargs.get("filter") else list(self.documents.values())
        return [(doc, .25) for doc in selected[:kwargs.get("k", 50)]]


class Sources:
    def __init__(self, members):
        self.members = members

    def identity(self, uid):
        return self.members.get(uid)

    def bounded(self, *args):
        return []


def test_publish_replacement_and_removal_never_return_stale_data():
    store, vectors = MemoryStore(), Vectors()
    index = KnowledgeIndex(store, None, vectors)
    old = record()
    store.put("ledger_knowledge", old)
    index.publish(old)
    evidence = index.retrieve("blade")
    assert len(evidence) == 1 and index.valid_evidence(evidence)
    replacement = record(generation="v2")
    replacement.pop("indexed_generation")
    store.put("ledger_knowledge", replacement)
    assert index.retrieve("blade") == []
    assert not index.valid_evidence(evidence)
    index.publish(replacement)
    assert index.retrieve("blade") == []
    replacement["indexed_generation"] = "v2"
    store.put("ledger_knowledge", replacement)
    assert index.retrieve("blade")[0]["generation"] == "v2"
    store.delete("ledger_knowledge", old["_id"])
    assert index.retrieve("blade") == []
    index.remove(old["_id"])
    assert not vectors.documents


def test_retrieval_filters_and_ranks_recency_then_verified_people():
    store, vectors = MemoryStore(), Vectors()
    recent = record(key="recent", users=["UADMIN"])
    old = record(key="old", date=datetime.now(timezone.utc) - timedelta(days=800))
    sources = Sources({"UADMIN": {"role": "admin"}})
    index = KnowledgeIndex(store, sources, vectors)
    for item in (old, recent):
        store.put("ledger_knowledge", item)
        index.publish(item)
    assert [item["thread_id"] for item in index.retrieve("saw")] == ["recent", "old"]
    assert index.retrieve("saw", channel_name="other") == []
    assert [x["thread_id"] for x in index.retrieve("saw", participant="uadmin")] == ["recent"]
    assert [x["thread_id"] for x in index.retrieve("saw", date_from=datetime.now(timezone.utc) - timedelta(days=3))] == ["recent"]
    assert index.retrieve("saw", min_similarity=.9) == []


def test_publication_racing_a_removal_cleans_its_own_generation():
    store, vectors = MemoryStore(), Vectors()
    item = record()
    store.put("ledger_knowledge", item)
    add = vectors.add_texts
    def racing_add(*args, **kwargs):
        add(*args, **kwargs)
        store.put("ledger_knowledge", {"_id": item["_id"], "status": "removed"})
    vectors.add_texts = racing_add
    with pytest.raises(RuntimeError):
        KnowledgeIndex(store, None, vectors).publish(item)
    assert not vectors.documents


def test_thread_url_rejects_untrusted_destinations():
    assert thread_url("C123", "1234.000001") == "https://app.slack.com/archives/C123/p1234000001"
    assert thread_url("C123/../evil", "1234.000001") is None
    assert thread_url("C123", "https://evil.test") is None


def test_only_verified_current_subscribers_receive_subscriber_boost():
    clock = datetime.now(timezone.utc).timestamp()
    store = MemoryStore()
    store.put("ledger_catalog", {"_id": "identity:1", "slack_id": "USUB", "at": datetime.now(timezone.utc), "deactivated": False, "bot": False})
    sources = Sources({"USUB": {"_id": "1", "status": "activeMember", "expirationTime": (clock + 60) * 1000,
                                "subscription_id": "x"},
                       "UEXPIRED": {"status": "activeMember", "expirationTime": (clock - 1) * 1000,
                                    "subscription_id": "x"},
                       "UPENDING": {"status": "pending", "expirationTime": (clock + 60) * 1000,
                                    "subscription_id": "x"}})
    assert _role_boost(sources, ["USUB"], clock + .1, store=store) == .04
    assert _role_boost(sources, ["UEXPIRED", "UPENDING", "UUNKNOWN"], clock, store=store) == 0


def test_metadata_filters_apply_before_the_candidate_limit():
    store, vectors = MemoryStore(), Vectors()
    index = KnowledgeIndex(store, None, vectors)
    earlier = datetime.now(timezone.utc) - timedelta(days=30)
    for i in range(55):
        row = record(key=f"old-{i}", date=earlier, users=["UOTHER"])
        store.put("ledger_knowledge", row)
        index.publish(row)
    target = record(key="target", users=["UTARGET"])
    target["messages"] = [{"display_name": "Pat Example"}]
    store.put("ledger_knowledge", target)
    index.publish(target)
    assert [x["thread_id"] for x in index.retrieve("saw", participant="  Pat Example ", date_from=earlier + timedelta(days=1))] == ["target"]


def test_ranking_rejects_stale_mismatched_and_nonhuman_cache():
    clock = datetime.now(timezone.utc).timestamp()
    store = MemoryStore()
    sources = Sources({"UADMIN": {"_id": "1", "role": "admin"}})
    base = {"_id": "identity:1", "slack_id": "UADMIN", "at": datetime.fromtimestamp(clock, timezone.utc), "deactivated": False, "bot": False}
    for change in ({"slack_id": "UOLD"}, {"at": datetime.fromtimestamp(clock - 86401, timezone.utc)},
                   {"bot": True}, {"deactivated": True}, {"bot": None}, {"deactivated": None}):
        store.put("ledger_catalog", {**base, **change})
        assert _role_boost(sources, ["UADMIN"], clock, store=store) == 0
    store.put("ledger_catalog", base)
    assert _role_boost(sources, ["UADMIN"], clock, store=store) == .10
    store.put("ledger_catalog", {**base, "slack_id": "UOLD"})
    assert _role_boost(sources, ["UADMIN"], clock, store=store, verify_human=lambda uid: True) == .10
    assert _role_boost(sources, ["UADMIN"], clock, store=store, verify_human=lambda uid: False) == 0


def test_live_slack_verification_is_bounded_and_fails_closed(monkeypatch):
    calls = []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"ok": True, "user": {"id": "U1", "deleted": False, "is_bot": False}}
    def get(url, **kwargs):
        calls.append(kwargs)
        return Response()
    monkeypatch.setattr("ledger.knowledge_index.requests.get", get)
    verify = _LiveHumanVerifier("private-token")
    assert verify("U1")
    assert not verify("U2")
    for _ in range(5):
        verify("U1")
    assert len(calls) == 5
    assert all(item["params"] == {"user": "U1"} or item["params"] == {"user": "U2"} for item in calls)
    Response.json = lambda self: {"ok": True, "user": {"id": "U1", "deleted": False}}
    assert not _LiveHumanVerifier("private-token")("U1")
    before = len(calls)
    assert not _LiveHumanVerifier("private-token", deadline=time.monotonic() - 1)("U1")
    assert len(calls) == before


def test_embedding_response_validation_and_task_prefix(monkeypatch):
    seen = []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return [[0.5, 0.7]]
    def post(url, json, timeout):
        seen.append((url, json))
        return Response()
    monkeypatch.setattr("ledger.knowledge_index.requests.post", post)
    model = PrivateEmbeddings("http://private")
    assert model.embed_query("saw") == [0.5, 0.7]
    assert seen[0][1] == {"inputs": ["search_query: saw"]}
    Response.json = lambda self: [[float("nan")]]
    with pytest.raises(ValueError):
        model.embed_documents(["saw"])
