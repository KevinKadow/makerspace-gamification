import json

import pytest

from conftest import oid
from ledger.conversations import converse, sponsorship_request
from ledger.domain import Denied
from ledger.query_tools import QueryTools
from ledger.sponsorships import (build_report, caller_invitation, invitation_key, render_report)
from ledger.sponsorships import valid_opener
from ledger.worker import Worker


def mid(number):
    return str(oid(number))


def test_each_inviter_is_recorded_while_first_sponsor_keeps_credit(env):
    ledger, store, *_ = env
    ledger.join(mid(1))
    ledger.join(mid(2))

    ledger.sponsor(mid(1), mid(3))
    ledger.sponsor(mid(2), mid(3))
    ledger.sponsor(mid(2), mid(3))

    assert store.get("ledger_relationships", f"sponsor:{mid(3)}")["giver"] == mid(1)
    assert caller_invitation(ledger, mid(1), mid(3))["source"] == "sponsor_command"
    assert caller_invitation(ledger, mid(2), mid(3))["source"] == "sponsor_command"
    assert len(store.select("ledger_relationships", {"kind": "sponsor_invitation"})) == 2
    assert len(store.select("ledger_outbox", {"kind": "message", "payload.type": "invitation"})) == 2

    ledger.join(mid(3), mid(2))
    canonical = store.get("ledger_relationships", f"sponsor:{mid(3)}")
    assert canonical["giver"] == mid(1) and canonical["status"] == "accepted"


def test_kudos_invitation_and_legacy_backfill_use_pair_history(env):
    ledger, store, *_ = env
    ledger.join(mid(1))
    ledger._sponsor(mid(1), mid(3), notify=False, source="kudos_invitation")
    assert store.get("ledger_relationships", invitation_key(mid(1), mid(3)))["source"] == "kudos_invitation"

    store.put("ledger_relationships", {"_id": f"sponsor:{mid(4)}", "kind": "sponsor",
        "giver": mid(1), "recipient": mid(4), "status": "pending", "at": ledger.participant(mid(1))["first_opt_in"]})
    assert ledger.backfill_sponsor_invitations() == 1
    assert ledger.backfill_sponsor_invitations() == 0
    migrated = store.get("ledger_relationships", invitation_key(mid(1), mid(4)))
    assert migrated["source"] == "sponsor_command" and migrated["legacy_backfill"] is True


def test_report_joins_current_consent_and_latest_dates(env):
    ledger, *_ = env
    ledger.join(mid(1))
    ledger.sponsor(mid(1), mid(3))
    never = build_report(ledger, mid(1), "detail", recipient=mid(3))["rows"][0]
    assert never["status"] == "Never opted in" and never["latest_opt_in"] == "—"

    ledger.join(mid(3), mid(1))
    joined = build_report(ledger, mid(1), "detail", recipient=mid(3))["rows"][0]
    assert joined["status"] == "Opted in" and joined["latest_opt_in"] != "—"

    ledger.leave(mid(3))
    left = build_report(ledger, mid(1), "detail", recipient=mid(3))["rows"][0]
    assert left["status"] == "Opted out" and left["latest_opt_out"] != "—"

    ledger.join(mid(3), mid(1))
    returned = build_report(ledger, mid(1), "detail", recipient=mid(3))["rows"][0]
    assert returned["status"] == "Opted in"
    assert returned["latest_opt_in"] != "—" and returned["latest_opt_out"] != "—"


def test_report_table_pages_preserve_every_row():
    rows = [{"recipient": str(index), "name": f"Maker {index}", "invited": "Oct 7, 2026",
             "status": "Never opted in", "latest_opt_in": "—", "latest_opt_out": "—"}
            for index in range(205)]
    pages = render_report({"mode": "list", "status": "ok", "rows": rows, "totals": {}, "as_of": "now"})
    rendered = [cell["text"] for page in pages for row in page["blocks"][1]["rows"][1:] for cell in row]
    assert len(pages) >= 3
    assert all(len(page["blocks"][1]["rows"]) <= 100 for page in pages)
    assert all(page["blocks"][1]["column_settings"] == [
        {"is_wrapped": True}, {"is_wrapped": False}, {"is_wrapped": True},
        {"is_wrapped": False}, {"is_wrapped": False}] for page in pages)
    assert all(f"Maker {index}" in rendered for index in range(205))


def test_summary_table_has_one_object_setting_per_column():
    report = {"mode": "summary", "status": "ok", "rows": [],
        "totals": {"Invited": 2, "Opted in": 1}, "as_of": "now"}
    table = render_report(report)[0]["blocks"][1]
    assert len(table["column_settings"]) == len(table["rows"][0]) == 2
    assert table["column_settings"] == [{"is_wrapped": True}, {"is_wrapped": False}]
    assert all(isinstance(setting, dict) for setting in table["column_settings"])


def test_report_fallback_escapes_slack_markup_but_table_keeps_raw_name():
    name = "Maker <!here> <@U123> & Friends"
    report = {"mode": "list", "status": "ok", "rows": [{"recipient": mid(3), "name": name,
        "invited": "Oct 7, 2026", "status": "Never opted in", "latest_opt_in": "—", "latest_opt_out": "—"}],
        "totals": {}, "as_of": "now"}
    page = render_report(report)[0]
    assert "<!here>" not in page["text"] and "<@U123>" not in page["text"]
    assert "&lt;!here&gt;" in page["text"] and "&lt;@U123&gt;" in page["text"] and "&amp; Friends" in page["text"]
    assert page["blocks"][1]["rows"][1][0]["text"] == name


def test_sponsor_opener_uses_affirmative_fact_free_allowlist():
    report = {"rows": [{"name": "Alex Maker"}]}
    assert valid_opener("The Ledger opens your private sponsorship register.", report)
    for text in ("Alex Maker appears in the register.", "Three invitations await.",
                 "Someone opted in today.", "The current status is ready.",
                 "Your invitations have been successful.", "Every invitation was ignored.",
                 "Your invitees are still waiting to respond.", "A custom but vague introduction."):
        assert not valid_opener(text, report)


def test_sponsor_commands_queue_private_list_and_existing_detail(env):
    ledger, store, _, composer, _, slack = env
    ledger.join(mid(1))
    ledger.sponsor(mid(1), mid(3))
    invitation_jobs = len(store.select("ledger_outbox", {"kind": "message", "payload.type": "invitation"}))
    worker = Worker(ledger, composer, slack)

    worker.command(mid(1), "/ledger sponsor", "command:list")
    worker.command(mid(1), "/ledger sponsor <@U3>", "command:detail")
    jobs = store.select("ledger_outbox", {"kind": "sponsor_report"})
    assert {job["payload"]["mode"] for job in jobs} == {"list", "detail"}
    assert len(store.select("ledger_outbox", {"kind": "message", "payload.type": "invitation"})) == invitation_jobs

    job = store.claim("ledger_outbox", kinds=["sponsor_report"])
    worker.outbox(job)
    call = slack.chat_postMessage.call_args.kwargs
    assert call["channel"] == "DU1" and any(block["type"] == "table" for block in call["blocks"])


def test_unrecorded_active_target_is_generic_and_not_added(joined):
    ledger, store, _, composer, _, slack = joined
    Worker(ledger, composer, slack).command(mid(1), "/ledger sponsor <@U2>", "command:active")
    assert not caller_invitation(ledger, mid(1), mid(2))
    response = store.get("ledger_outbox", "dm:command:active")
    assert response["payload"]["facts"] == {"summary": "That member cannot receive a sponsorship invitation."}


def test_private_sponsorship_tool_is_caller_scoped_and_qwen_only_opens(env):
    ledger, _, _, composer, api, _ = env
    ledger.join(mid(1))
    ledger.sponsor(mid(1), mid(3))
    tool = QueryTools(ledger, mid(1), private=True)
    result = tool.call("my_sponsorships", {"mode": "detail", "invitee": "Maker3"})
    assert result["status"] == "ok" and tool.sponsorship_report["rows"][0]["name"] == "Maker3 Test"
    missing = QueryTools(ledger, mid(1), private=True)
    assert missing.call("my_sponsorships", {"mode": "detail", "invitee": "Maker2"})["status"] == "not_found"
    assert missing.sponsorship_report["rows"] == []
    with pytest.raises(Denied):
        QueryTools(ledger, mid(1), private=False).call("my_sponsorships", {"mode": "list"})

    api.tool_response.side_effect = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "sponsor-read", "type": "function",
            "function": {"name": "my_sponsorships", "arguments": json.dumps({"mode": "list"})}}]},
        {"role": "assistant", "content": "3 members opted in."},
    ]
    response = converse(ledger, composer, mid(1), "Who have I sponsored?", private=True)
    assert response["text"] == "The Ledger opens your private sponsorship register."
    assert response["sponsorship_report"]["rows"][0]["recipient"] == mid(3)
    tools = api.tool_response.call_args_list[0].args[1]
    assert any(item["function"]["name"] == "my_sponsorships" for item in tools)


@pytest.mark.parametrize("prompt", ["Who have I sponsored?", "Did someone I invited opt in?",
                                    "Show my invites", "List my invited members",
                                    "What invitations have I sent?"])
def test_sponsorship_question_rejects_answer_without_tool_result(env, prompt):
    ledger, _, _, composer, api, _ = env
    ledger.join(mid(1))
    ledger.sponsor(mid(1), mid(3))
    api.tool_response.return_value = {"role": "assistant", "content": "Maker3 opted in yesterday."}
    response = converse(ledger, composer, mid(1), prompt, private=True)
    assert response["outcome"] == "fallback"
    assert response["text"] == "Use /ledger sponsor to review your private sponsorship register."
    assert "sponsorship_report" not in response


@pytest.mark.parametrize("prompt", ["Show my invites", "List my invited members",
                                    "What invitations have I sent?", "List invitations I sent",
                                    "Show invitations I've sent", "What invitations did I send?"])
def test_common_outgoing_invitation_phrases_require_the_private_register(prompt):
    assert sponsorship_request(prompt)


def test_contextual_sponsorship_followup_can_refresh_the_private_register(env):
    ledger, _, _, composer, api, _ = env
    ledger.join(mid(1))
    ledger.sponsor(mid(1), mid(3))
    history = [{"role": "user", "content": "Who have I sponsored?"},
               {"role": "assistant", "content": "The Ledger opens your private sponsorship register."}]
    api.tool_response.side_effect = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "sponsor-followup", "type": "function",
            "function": {"name": "my_sponsorships", "arguments": json.dumps({"mode": "list"})}}]},
        {"role": "assistant", "content": "The Ledger checks the register again."},
    ]
    response = converse(ledger, composer, mid(1), "Which of them opted in?", history=history, private=True)
    assert response["outcome"] == "generated"
    assert response["sponsorship_report"]["rows"][0]["recipient"] == mid(3)


def test_contextual_sponsorship_followup_rejects_unverified_direct_answer(env):
    ledger, _, _, composer, api, _ = env
    ledger.join(mid(1))
    history = [{"role": "user", "content": "Show my invites"},
               {"role": "assistant", "content": "The Ledger opens your private sponsorship register."}]
    api.tool_response.return_value = {"role": "assistant", "content": "Someone opted in."}
    response = converse(ledger, composer, mid(1), "Which of them opted in?", history=history, private=True)
    assert response["outcome"] == "fallback"
    assert response["text"] == "Use /ledger sponsor to review your private sponsorship register."


@pytest.mark.parametrize("prompt", ["When did I opt in?", "Was I opted out yesterday?"])
def test_self_consent_questions_are_not_sponsorship_requests(env, prompt):
    ledger, _, _, composer, api, _ = env
    ledger.join(mid(1))
    assert not sponsorship_request(prompt)
    api.tool_response.return_value = {"role": "assistant", "content": "That question concerns your own participation history."}
    response = converse(ledger, composer, mid(1), prompt, private=True)
    assert response["outcome"] == "generated"
    assert response["text"] == "That question concerns your own participation history."
    assert "sponsorship_report" not in response


@pytest.mark.parametrize("prompt", ["Who sponsored me?", "Who invited me?", "Who is my sponsor?"])
def test_incoming_invitation_questions_do_not_use_outgoing_register(env, prompt):
    ledger, _, _, composer, api, _ = env
    ledger.join(mid(1))
    assert not sponsorship_request(prompt)
    api.tool_response.return_value = {"role": "assistant", "content": "That asks about an invitation you received."}
    response = converse(ledger, composer, mid(1), prompt, private=True)
    assert response["outcome"] == "generated"
    assert response["text"] == "That asks about an invitation you received."
    assert "sponsorship_report" not in response


def test_incoming_invitation_rejects_model_attempt_to_use_outgoing_tool(env):
    ledger, _, _, composer, api, _ = env
    ledger.join(mid(1))
    api.tool_response.return_value = {"role": "assistant", "content": None, "tool_calls": [{"id": "wrong-direction",
        "type": "function", "function": {"name": "my_sponsorships", "arguments": json.dumps({"mode": "list"})}}]}
    response = converse(ledger, composer, mid(1), "Who invited me?", private=True)
    assert response["outcome"] == "fallback" and response["text"] == "I don't know."
    assert "sponsorship_report" not in response


def test_opt_out_cancels_pending_sponsor_report(env):
    ledger, store, _, composer, _, slack = env
    ledger.join(mid(1))
    Worker(ledger, composer, slack).command(mid(1), "/ledger sponsor", "command:cancel")
    ledger.leave(mid(1))
    assert store.get("ledger_outbox", "sponsor-report:command:cancel")["status"] == "cancelled"


def test_sponsorship_words_enable_private_conversation_tools_without_question_mark(env):
    ledger, store, _, composer, _, slack = env
    ledger.join(mid(1))
    outcome = Worker(ledger, composer, slack).event({"type": "message", "channel": "DU1", "channel_type": "im",
        "user": "U1", "text": "List my sponsorships", "ts": "100.200"}, "slack:event:sponsor-list")
    assert outcome == "reply_queued"
    job = store.get("ledger_outbox", "reply:DU1:100.200")
    assert job["payload"]["use_tools"] is True and job["payload"]["slack_id"] == "U1"


@pytest.mark.parametrize("text", [
    "List everyone I invite",
    "List everyone I invited",
    "Show the people I am inviting",
])
def test_sponsor_invite_verbs_enable_tools_without_question_mark(env, text):
    ledger, store, _, composer, _, slack = env
    ledger.join(mid(1))
    outcome = Worker(ledger, composer, slack).event({"type": "message", "channel": "DU1", "channel_type": "im",
        "user": "U1", "text": text, "ts": "100.201"}, "slack:event:sponsor-invite-verb")
    assert outcome == "reply_queued"
    job = store.get("ledger_outbox", "reply:DU1:100.201")
    assert job["payload"]["use_tools"] is True and job["payload"]["slack_id"] == "U1"
