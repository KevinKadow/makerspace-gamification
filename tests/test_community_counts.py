from datetime import datetime, timezone
import pytest

from ledger.community_counts import bounds, clarification, count, format_answer, is_count_question, recognize, safe_count, valid_space_answer
from ledger.worker import Worker


def test_recognizes_only_supported_unambiguous_public_count_requests():
    assert recognize("How busy is the space right now?") == ("space", "right_now")
    assert recognize("How many visitors came yesterday?") == ("space", "yesterday")
    assert recognize("How many new members joined this week?") == ("members", "this_week")
    assert recognize("How many new members joined last week?") is None
    assert recognize("How many new members joined today and this week?") is None
    assert recognize("How busy is the space and how many new members joined today?") is None
    assert recognize("How many people were here?") is None
    assert recognize("How many people registered for the class today?") is None
    assert not is_count_question("How many people registered for the class today?")
    assert recognize("How many people registered for the class at the makerspace today?") is None
    assert not is_count_question("How many people registered for the class at the makerspace today?")
    assert recognize("How many people are here today?") == ("space", "today")
    assert recognize("How many people visited the makerspace today?") == ("space", "today")
    assert recognize("How many people have visited the space this week?") == ("space", "this_week")
    assert recognize("How many people came to the makerspace yesterday?") == ("space", "yesterday")
    assert recognize("How many people checked in here this month?") == ("space", "this_month")
    assert recognize("How many people have come to the space today?") == ("space", "today")
    assert recognize("How many people have checked in at the makerspace today?") == ("space", "today")
    assert recognize("How many people came here today?") == ("space", "today")
    assert recognize("How many people have come here today?") == ("space", "today")
    assert recognize("How many people came to here today?") is None
    assert recognize("How many people visited the makerspace website today?") is None
    assert not is_count_question("How many people visited the makerspace website today?")
    assert recognize("How many people came to the space station today?") is None
    assert not is_count_question("How many people came to the space station today?")
    assert recognize("How many people were at the space station today?") is None
    assert recognize("How busy is the makerspace website today?") is None
    assert not is_count_question("How busy is the makerspace website today?")
    assert recognize("How many visitors did the website get today?") is None
    assert not is_count_question("How many visitors did the website get today?")
    assert recognize("How many visitors did the station get today?") is None
    assert not is_count_question("How many visitors did the station get today?")
    assert recognize("How many check-ins did the web page record today?") is None
    assert not is_count_question("How many check-ins did the web page record today?")
    assert recognize("How many check-ins were recorded today?") == ("space", "today")
    assert is_count_question("How many new members joined last week?")
    assert "Which period" in clarification("How many new members joined last week?")
    space_clarification = clarification("How busy is the space?")
    assert "members who used the space" in space_clarification and "UID" not in space_clarification


def test_period_bounds_are_new_york_local_and_half_open():
    instant = datetime(2026, 10, 6, 16, 30, tzinfo=timezone.utc)
    start, end = bounds("today", instant)
    assert start == datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc)
    start, end = bounds("right_now", instant)
    assert (end - start).total_seconds() == 7200


def test_fixed_aggregations_return_count_only_and_format_estimate():
    class Sources:
        def __init__(self):
            self.calls = []

        def _aggregate(self, collection, pipeline, timeout_ms):
            self.calls.append((collection, pipeline, timeout_ms))
            return [{"count": 3}]

    source = Sources()
    instant = datetime(2026, 10, 6, 16, 30, tzinfo=timezone.utc)
    result = count(source, "space", "right_now", instant)
    assert result == {"subject": "space", "period": "right_now", "count": 3}
    collection, pipeline, timeout_ms = source.calls[0]
    assert collection == "checkins" and timeout_ms == 2000
    assert pipeline[0]["$match"]["uid"] == {"$type": "string", "$ne": ""}
    assert len(pipeline[0]["$match"]["$or"]) == 3
    assert pipeline[0]["$match"]["$or"][0]["timeOf"]["$gte"] == int((instant.timestamp() - 7200) * 1000)
    assert pipeline[0]["$match"]["$or"][1]["timeOf"]["$gte"] == int(instant.timestamp() - 7200)
    assert pipeline[1] == {"$project": {"_id": 0, "uid": 1}}
    assert pipeline[2] == {"$group": {"_id": "$uid"}}
    assert format_answer(result) == ("A total of 3 members used the space in the last two hours. "
                                      "This is a recent-use estimate, not a live occupancy count.")
    assert valid_space_answer(format_answer(result), result)
    assert not valid_space_answer("3 unique check-in UIDs were recorded in the last two hours.", result)
    assert not valid_space_answer("A total of 4 members used the space in the last two hours. This is a recent-use estimate, not a live occupancy count.", result)
    assert not valid_space_answer("3 members did not use the space in the last two hours. This is a recent-use estimate, not a live occupancy count.", result)
    assert not valid_space_answer("More than 3 members used the space in the last two hours. This is a recent-use estimate, not a live occupancy count.", result)
    assert not valid_space_answer("Approximately 3 members used the space in the last two hours. This is a recent-use estimate, not a live occupancy count.", result)

    today = {"subject": "space", "period": "today", "count": 3}
    assert valid_space_answer("Today, the space welcomed a total of 3 members.", today)
    assert not valid_space_answer("At least 3 members visited the makerspace today.", today)
    assert not valid_space_answer("3 members used the space today, perhaps more.", today)

    member_result = count(source, "members", "today", instant)
    assert member_result["count"] == 3
    member_pipeline = source.calls[-1][1]
    assert member_pipeline[0]["$match"]["merged_at"] is None
    assert member_pipeline[1] == {"$project": {"_id": 0, "startDate": 1}}


def test_failed_source_query_returns_unavailable_without_error_details():
    class Sources:
        def _aggregate(self, *_args, **_kwargs):
            raise TimeoutError("private query details")

    assert safe_count(Sources(), "space", "today") is None


def test_unlinked_user_gets_public_count_in_any_joined_channel(env, monkeypatch):
    ledger, store, source, composer, api, slack = env
    worker = Worker(ledger, composer, slack, bot_id="UBOT")
    calls = []

    def aggregate(collection, pipeline, timeout_ms=2000):
        calls.append((collection, pipeline, timeout_ms))
        return [{"count": 4}]

    monkeypatch.setattr(source, "_aggregate", aggregate, raising=False)
    monkeypatch.setattr(source, "identity", lambda _slack_id: (_ for _ in ()).throw(AssertionError("identity lookup")))
    result = worker.event({"type": "message", "user": "U_UNLINKED", "channel": "C_OTHER",
                           "ts": "123.456", "text": "How busy has the space been today?"}, "evt")
    assert result == "community_count_queued"
    assert calls[0][0] == "checkins"
    assert slack.conversations_info.call_count == 2
    slack.chat_postMessage.assert_not_called()
    api.complete.return_value = "The space welcomed a total of 4 members today."
    job = store.claim("ledger_outbox", kinds=["community_count_reply"])
    worker.outbox(job)
    reply = slack.chat_postMessage.call_args.kwargs
    assert reply["channel"] == "C_OTHER" and reply["thread_ts"] == "123.456"
    assert reply["reply_broadcast"] is True
    assert reply["text"] == "The space welcomed a total of 4 members today."
    rendered = "\n".join(message["content"] for message in api.complete.call_args.args[0])
    assert "How busy has the space been today?" not in rendered and "U_UNLINKED" not in rendered
    assert not store.select("ledger_context", {"kind": "message"})


@pytest.mark.parametrize("generated", [
    "13 unique check-in UIDs were recorded today.",
    "12 members did not use the space today.",
    "More than 12 members used the space today.",
])
def test_space_count_uses_authoritative_fallback_when_qwen_changes_facts(env, monkeypatch, generated):
    ledger, store, source, composer, api, slack = env
    worker = Worker(ledger, composer, slack, bot_id="UBOT")
    monkeypatch.setattr(source, "_aggregate", lambda *_args, **_kwargs: [{"count": 12}], raising=False)
    api.complete.return_value = generated
    assert worker.event({"type": "message", "user": "U_OTHER", "channel": "DU_OTHER",
                         "channel_type": "im", "ts": "2", "text": "How busy is the space today?"}, "evt") == "community_count_queued"
    worker.outbox(store.claim("ledger_outbox", kinds=["community_count_reply"]))
    assert slack.chat_postMessage.call_args.kwargs["text"] == "A total of 12 members used the space today."


def test_count_path_ignores_unjoined_channels_and_non_count_ambient_questions(env, monkeypatch):
    ledger, _, source, _, _, slack = env
    worker = Worker(ledger, None, slack, bot_id="UBOT")
    monkeypatch.setattr(source, "_aggregate", lambda *_args, **_kwargs: [{"count": 2}], raising=False)
    slack.conversations_info.return_value = {"channel": {"is_member": False}}
    assert worker.event({"type": "message", "user": "U_UNLINKED", "channel": "C_OTHER",
                         "ts": "1", "text": "How busy is the space today?"}, "evt") == "ignored_community_count_unjoined_channel"
    slack.chat_postMessage.assert_not_called()
    slack.conversations_info.reset_mock()
    slack.conversations_info.return_value = {"channel": {"is_member": True}}
    assert worker.event({"type": "message", "user": "U_UNLINKED", "channel": "C_OTHER",
                         "ts": "2", "text": "What time does the shop close?"}, "evt2") == "ignored_unlinked_identity"
    slack.chat_postMessage.assert_not_called()
