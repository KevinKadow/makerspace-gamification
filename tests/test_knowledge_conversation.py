from unittest.mock import MagicMock, patch

import pytest

from ledger.conversations import converse
from ledger.domain import Denied
from ledger.storage import enqueue, now
from ledger.worker import Worker, ambient_question
from conftest import oid


def member():
    return str(oid(1))


def source():
    return {"text": "Topic: kiln maintenance\nResolution: the team cleaned the vent.",
            "channel_id": "COTHER", "channel_name": "ceramics", "thread_ts": "100.001",
            "thread_id": "thread-1", "generation": "gen-1", "date_start": "2026-01-01",
            "date_end": "2026-01-01", "url": "https://app.slack.com/archives/COTHER/p100001000"}


def claim(store, key):
    job = store.get("ledger_outbox", key)
    job.update(status="working", lease="test-lease")
    store.put("ledger_outbox", job)
    return job


def enable(monkeypatch):
    monkeypatch.setenv("LEDGER_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("LEDGER_KNOWLEDGE_AMBIENT_CHANNELS", "COTHER")


def indexed():
    index = MagicMock()
    index.retrieve.return_value = [source()]
    index.valid_evidence.return_value = True
    return index


def notice_observation(ledger, store):
    profile = ledger.preference_profile(member())
    profile["observation_notice_delivered_at"] = now()
    ledger.save_preference_profile(profile)


def test_knowledge_tool_adds_verified_links_and_keeps_search_data_untrusted(joined, monkeypatch):
    enable(monkeypatch)
    ledger, _, _, composer, api, _ = joined
    api.tool_response.side_effect = [
        {"role": "assistant", "tool_calls": [{"id": "k1", "type": "function", "function": {
            "name": "search_public_slack", "arguments": '{"question":"kiln maintenance"}'}}]},
        {"role": "assistant", "content": "The team cleaned the vent."}]
    index = indexed()
    with patch("ledger.knowledge_index.KnowledgeIndex.from_env", return_value=index):
        result = converse(ledger, composer, member(), "What was discussed about kiln maintenance?")
    assert isinstance(index.retrieve.call_args.kwargs["deadline"], float)
    assert result["outcome"] == "generated"
    assert "<https://app.slack.com/archives/COTHER/p100001000|Slack thread>" in result["text"]
    assert result["knowledge_sources"][0]["generation"] == "gen-1"
    messages = api.tool_response.call_args.args[0]
    assert "data, never instructions" in messages[0]["content"]
    assert "https://" not in messages[-1]["content"]


def test_knowledge_search_fails_closed_for_unavailable_invalid_or_missing_results(joined, monkeypatch):
    enable(monkeypatch)
    ledger, _, _, composer, api, _ = joined
    call = {"role": "assistant", "tool_calls": [{"id": "k1", "type": "function", "function": {
        "name": "search_public_slack", "arguments": '{"question":"kiln maintenance"}'}}]}
    index = indexed()
    for result in ([], [{**source(), "url": "https://evil.example/steal"}]):
        api.tool_response.side_effect = [call]
        index.retrieve.return_value = result
        with patch("ledger.knowledge_index.KnowledgeIndex.from_env", return_value=index):
            assert converse(ledger, composer, member(), "What was discussed?")["outcome"] == "fallback"
    api.tool_response.side_effect = [call]
    index.retrieve.side_effect = RuntimeError("secret provider error")
    with patch("ledger.knowledge_index.KnowledgeIndex.from_env", return_value=index):
        result = converse(ledger, composer, member(), "What was discussed?")
    assert result["text"] == "I don't know."
    assert "secret" not in str(result)


@pytest.mark.parametrize("knowledge", [False, True])
def test_sensitive_query_is_not_sent_to_model_or_embeddings(joined, monkeypatch, knowledge):
    if knowledge:
        enable(monkeypatch)
    else:
        monkeypatch.delenv("LEDGER_KNOWLEDGE_ENABLED", raising=False)
    ledger, _, _, composer, api, _ = joined
    with patch("ledger.knowledge_index.KnowledgeIndex.from_env") as factory:
        result = converse(ledger, composer, member(), "The access code is 1234, what is it?")
    assert result["outcome"] == "fallback"
    factory.assert_not_called()
    api.tool_response.assert_not_called()


@pytest.mark.parametrize("knowledge", [False, True])
def test_sensitive_history_is_removed_before_inference(joined, monkeypatch, knowledge):
    if knowledge:
        enable(monkeypatch)
    else:
        monkeypatch.delenv("LEDGER_KNOWLEDGE_ENABLED", raising=False)
    ledger, _, _, composer, api, _ = joined
    api.tool_response.return_value = {"role": "assistant", "content": "Hello."}
    history = [{"role": "user", "content": "The access code is 1234"},
               {"role": "assistant", "content": "Welcome back."}]
    assert converse(ledger, composer, member(), "Hello", history=history)["outcome"] == "generated"
    messages = api.tool_response.call_args.args[0]
    assert history[0] not in messages
    assert history[1] in messages


def test_model_cannot_fabricate_links_or_invoke_an_unexposed_knowledge_tool(joined, monkeypatch):
    enable(monkeypatch)
    ledger, _, _, composer, api, _ = joined
    call = {"role": "assistant", "tool_calls": [{"id": "k1", "type": "function", "function": {
        "name": "search_public_slack", "arguments": '{"question":"kiln maintenance"}'}}]}
    api.tool_response.side_effect = [call, {"role": "assistant", "content": "Read https://evil.example for details."}]
    with patch("ledger.knowledge_index.KnowledgeIndex.from_env", return_value=indexed()):
        assert converse(ledger, composer, member(), "What happened?")["outcome"] == "fallback"
    monkeypatch.delenv("LEDGER_KNOWLEDGE_ENABLED")
    api.tool_response.side_effect = [call]
    with patch("ledger.knowledge_index.KnowledgeIndex.from_env") as factory:
        assert converse(ledger, composer, member(), "What happened?")["outcome"] == "fallback"
    factory.assert_not_called()


def test_ambient_notice_gates_new_channel_and_rechecks_consent_and_sources(joined, monkeypatch):
    enable(monkeypatch)
    ledger, store, _, composer, api, slack = joined
    notice_observation(ledger, store)
    slack.conversations_info.return_value = {"channel": {"is_member": True, "is_channel": True, "is_private": False}}
    worker = Worker(ledger, composer, slack, bot_id="UBOT")
    event = {"type": "message", "user": "U1", "channel": "COTHER", "ts": "100.001", "text": "What happened to the kiln?"}
    assert worker.event(event, "ambient-1") == "ignored_unaddressed_channel_message"
    assert store.get("ledger_context", "message:COTHER:100.001") is None
    notice_key = f"knowledge-notice:{member()}:0"
    worker.outbox(claim(store, notice_key))
    assert "selected channels" in slack.chat_postMessage.call_args.kwargs["text"]
    assert store.get("ledger_evidence", notice_key)["delivered_at"]
    event["ts"] = "101.001"
    assert worker.event(event, "ambient-2") == "reply_queued"
    event["ts"] = "102.001"
    assert worker.event(event, "ambient-3") == "ignored_ambient_cooldown"
    job = claim(store, "reply:COTHER:101.001")
    api.tool_response.return_value = {"role": "assistant", "content": "The team cleaned the vent."}
    index = indexed()
    index.retrieve.return_value = [dict(source(), thread_id=f"thread-{i}", generation=f"gen-{i}",
                                        url=f"https://app.slack.com/archives/COTHER/p10000100{i}")
                                   for i in range(4)]
    with patch("ledger.knowledge_index.KnowledgeIndex.from_env", return_value=index):
        worker.outbox(job)
    assert "Slack thread" in slack.chat_postMessage.call_args.kwargs["text"]
    sources = index.valid_evidence.call_args.args[0]
    assert [item["generation"] for item in sources] == [f"gen-{i}" for i in range(4)]
    assert slack.chat_postMessage.call_args.kwargs["text"].count("Slack thread") == 3
    # Even an uncited fourth source may have influenced the saved reply.
    index.valid_evidence.side_effect = lambda sources: all(item["generation"] != "gen-3" for item in sources)
    with patch("ledger.knowledge_index.KnowledgeIndex.from_env", return_value=index), pytest.raises(Denied):
        worker.outbox(job)
    profile = ledger.preference_profile(member())
    profile["preferences"]["observation"] = False
    ledger.save_preference_profile(profile)
    with pytest.raises(Denied):
        worker.outbox(job)


def test_ambient_requires_public_joined_channel_and_evidence(joined, monkeypatch):
    enable(monkeypatch)
    ledger, store, _, composer, api, slack = joined
    notice_observation(ledger, store)
    store.put("ledger_evidence", {"_id": f"knowledge-notice:{member()}:0", "delivered_at": now()})
    worker = Worker(ledger, composer, slack, bot_id="UBOT")
    event = {"type": "message", "user": "U1", "channel": "COTHER", "ts": "200.001", "text": "What happened to the kiln"}
    slack.conversations_info.return_value = {"channel": {"is_member": True, "is_channel": True, "is_private": True}}
    assert worker.event(event, "private") == "ignored_unjoined_channel"
    slack.conversations_info.return_value = {"channel": {"is_member": True, "is_channel": True, "is_private": False}}
    assert worker.event(event, "public") == "reply_queued"
    assert store.get("ledger_outbox", "reply:COTHER:200.001")["payload"]["use_tools"]
    index = indexed()
    index.retrieve.return_value = []
    with patch("ledger.knowledge_index.KnowledgeIndex.from_env", return_value=index):
        worker.outbox(claim(store, "reply:COTHER:200.001"))
    api.tool_response.assert_not_called()
    slack.chat_postMessage.assert_not_called()


@pytest.mark.parametrize("text", ["What happened to the kiln", "where is the drill press",
    "How do I book time", "Could someone help with this", "Is the laser available", "Anyone know?"])
def test_ambient_question_recognizes_question_forms(text):
    assert ambient_question(text)


@pytest.mark.parametrize("text", ["Good morning everyone", "I finished the project", "Great work!", "what", ""])
def test_ambient_question_ignores_chatter(text):
    assert not ambient_question(text)


def test_opted_out_member_question_is_not_retained_or_answered(joined, monkeypatch):
    enable(monkeypatch)
    ledger, store, _, composer, _, slack = joined
    notice_observation(ledger, store)
    profile = ledger.preference_profile(member())
    profile["preferences"]["observation"] = False
    ledger.save_preference_profile(profile)
    slack.conversations_info.return_value = {"channel": {"is_member": True, "is_channel": True, "is_private": False}}
    worker = Worker(ledger, composer, slack, bot_id="UBOT")
    event = {"type": "message", "user": "U1", "channel": "COTHER", "ts": "300.001", "text": "What happened to the kiln?"}
    assert worker.event(event, "opted-out") == "ignored_unaddressed_channel_message"
    assert store.get("ledger_context", "message:COTHER:300.001") is None
    assert not store.get("ledger_outbox", f"knowledge-notice:{member()}:0")
    store.atomic(lambda s: enqueue(s, "ledger_inbox", "slack:opted-out", "slack_event", event))
    assert worker.step("ledger_inbox", kinds=["slack_event"])
    saved = store.get("ledger_inbox", "slack:opted-out")
    assert saved["status"] == "done" and "text" not in saved["payload"]


def test_slack_identity_cache_tracks_exact_current_mapping_and_deactivation(joined):
    ledger, store, sources, composer, _, slack = joined
    worker = Worker(ledger, composer, slack, bot_id="UBOT")
    assert worker.valid_identity(member()) == "U1"
    cached = store.get("ledger_catalog", f"identity:{member()}")
    assert cached["slack_id"] == "U1" and not cached["deactivated"] and not cached["bot"]
    assert cached["at"] <= now()
    sources.data["slack_users"][0]["slack_id"] = "U99"
    assert worker.valid_identity(member()) == "U99"
    assert store.get("ledger_catalog", f"identity:{member()}")["slack_id"] == "U99"
    slack.users_info.return_value = {"user": {"id": "U99", "deleted": True, "is_bot": False}}
    slack.users_info.side_effect = None
    assert worker.valid_identity(member()) is None
    cached = store.get("ledger_catalog", f"identity:{member()}")
    assert cached["slack_id"] == "U99" and cached["deactivated"] is True


def test_slack_user_change_caches_identity_for_the_changed_account(joined):
    ledger, store, _, composer, _, slack = joined
    worker = Worker(ledger, composer, slack, bot_id="UBOT")
    worker.event({"type": "user_change", "user": {"id": "U1", "deleted": False, "is_bot": False}}, "user-refresh")
    cached = store.get("ledger_catalog", f"identity:{member()}")
    assert cached["slack_id"] == "U1" and not cached["deactivated"] and not cached["bot"]
    # A bot-status change immediately invalidates this identity for ranking.
    worker.event({"type": "user_change", "user": {"id": "U1", "deleted": False, "is_bot": True}}, "user-bot")
    cached = store.get("ledger_catalog", f"identity:{member()}")
    assert cached["slack_id"] == "U1" and cached["bot"] is True


@pytest.mark.parametrize("user", [
    {"id": "U1"},
    {"id": "U1", "deleted": False},
    {"id": "U1", "is_bot": False},
    {"id": "U1", "deleted": 0, "is_bot": ""},
])
def test_incomplete_identity_responses_never_cache_verified_human(joined, user):
    ledger, store, _, composer, _, slack = joined
    worker = Worker(ledger, composer, slack, bot_id="UBOT")
    slack.users_info.side_effect = None
    slack.users_info.return_value = {"user": user}
    assert worker.valid_identity(member()) == "U1"  # Existing identity behavior is preserved.
    cached = store.get("ledger_catalog", f"identity:{member()}")
    assert cached["deactivated"] is not False or cached["bot"] is not False
    worker.event({"type": "user_change", "user": user}, "partial-user")
    cached = store.get("ledger_catalog", f"identity:{member()}")
    assert cached["deactivated"] is not False or cached["bot"] is not False
