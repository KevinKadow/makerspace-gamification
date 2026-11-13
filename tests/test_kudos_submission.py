import json
import time
from unittest.mock import patch

import pytest
from slack_sdk import WebClient

from conftest import oid
from ledger import kudos_submission, views
from ledger.http import HTTPApp
from ledger.slack_app import SlackUI, build_app
from ledger.storage import now
from ledger.worker import Worker
from test_slack import form, request


def modal_request(ledger, key="async-send", recipient=2, **fields):
    payload = form(views.kudos_form(ledger, str(oid(recipient)), key), {"message": "*Thanks* :hammer:", **fields})
    return {**payload, "type": "view_submission", "team": {"id": "T1"}}


def http(ui, store):
    return HTTPApp(build_app(ui, "xoxb-test", "test-signing-secret", "T1", "UBOT",
        WebClient(token="xoxb-test")), store)


def test_signed_send_ack_does_not_wait_for_identity_or_accounting(joined):
    ledger, store, _, composer, api, slack = joined
    ui = SlackUI(ledger, composer)
    app = http(ui, store)
    payload = modal_request(ledger)
    payload["response_url"] = "https://example.invalid/SECRET_RESPONSE_URL"
    meta = json.loads(payload["view"]["private_metadata"])
    payload["view"]["private_metadata"] = json.dumps({**meta, "private_secret": "SECRET_METADATA"})
    with patch.object(ui, "confirm_human", side_effect=AssertionError("Slow Slack request in ingress")), \
         patch.object(ledger, "kudos", side_effect=AssertionError("Slow accounting in ingress")):
        started = time.monotonic()
        for _ in range(2):
            assert request(app, {"payload": json.dumps(payload)}, "application/x-www-form-urlencoded", path="/slack/interactions")[0] == 200
        assert time.monotonic() - started < 3
    jobs = store.select("ledger_outbox", {"kind": "kudos_submit"})
    assert len(jobs) == 1 and not store.get("ledger_evidence", "kudos:async-send")
    assert "SECRET" not in json.dumps(jobs, default=str)
    worker = Worker(ledger, composer, slack)
    assert worker.step("ledger_outbox", kinds=["kudos_submit"])
    assert store.get("ledger_evidence", "kudos:async-send")["message"] == "*Thanks* :hammer:"
    assert not worker.step("ledger_outbox", kinds=["kudos_submit"])
    assert worker.step("ledger_outbox", kinds=["kudos_submission_reply"])
    assert "saved and queued" in slack.chat_postMessage.call_args.kwargs["text"]
    api.complete.assert_not_called()


def test_unpersisted_submission_is_not_acknowledged(joined):
    ledger, store, _, composer, *_ = joined
    app = http(SlackUI(ledger, composer), store)
    payload = modal_request(ledger)
    with patch.object(store, "atomic", side_effect=RuntimeError("Database unavailable")):
        assert request(app, {"payload": json.dumps(payload)}, "application/x-www-form-urlencoded", path="/slack/interactions")[0] >= 500
    assert not store.select("ledger_outbox", {"kind": "kudos_submit"})


@pytest.mark.parametrize("status", ["suspended", "revoked"])
def test_ineligible_mention_returns_deterministic_ephemeral_without_slack_lookup(joined, status):
    ledger, store, source, composer, api, _ = joined
    source.data["members"][1]["status"] = status
    ui = SlackUI(ledger, composer)
    with patch.object(ui, "confirm_human", side_effect=AssertionError("Ineligible recipient checked too late")):
        code, body = request(http(ui, store), {"team_id": "T1", "user_id": "U1", "command": "/kudos",
            "text": "<@U2|maker>", "trigger_id": "T"}, "application/x-www-form-urlencoded", path="/slack/commands")
    assert code == 200
    assert json.loads(body)["response_type"] == "ephemeral"
    assert "ineligible for kudos" in body and "suspended, revoked" in body
    api.complete.assert_not_called()


def test_ineligible_after_ack_is_reported_without_narration_and_draft_survives(joined):
    ledger, store, source, composer, api, slack = joined
    ui = SlackUI(ledger, composer)
    key = kudos_submission.reserve(ui, modal_request(ledger))
    source.data["members"][1]["status"] = "suspended"
    worker = Worker(ledger, composer, slack)
    with patch.object(SlackUI, "confirm_human", side_effect=AssertionError("Ineligible lookup")):
        assert worker.step("ledger_outbox", kinds=["kudos_submit"])
    assert not store.get("ledger_evidence", "kudos:async-send")
    assert worker.step("ledger_outbox", kinds=["kudos_submission_reply"])
    reply = slack.chat_postMessage.call_args.kwargs
    assert "ineligible" in reply["text"]
    action = reply["blocks"][-1]["elements"][0]
    body = {"user": {"id": "U1"}, "trigger_id": "T", "actions": [{"action_id": "kudos_retry", "value": action["value"]}]}
    ui.action(body, slack)
    view = slack.views_open.call_args.kwargs["view"]
    assert view["callback_id"] == "kudos_recipient"
    meta = json.loads(view["private_metadata"])
    assert ui.load_draft(str(oid(1)), meta["draft"])["message"] == "*Thanks* :hammer:"
    with pytest.raises(ValueError, match="belongs to another"):
        kudos_submission.review(ui, {**body, "user": {"id": "U2"}}, slack, key)
    api.complete.assert_not_called()


@pytest.mark.parametrize("flag", ["deleted", "is_bot"])
def test_unavailable_slack_recipient_review_preserves_draft(joined, flag):
    ledger, store, _, composer, api, slack = joined
    ui = SlackUI(ledger, composer)
    key = kudos_submission.reserve(ui, modal_request(ledger, public=True, emoji="hammer"))
    slack.users_info.side_effect = lambda user: {"user": {"id": user, flag: user == "U2"}}
    worker = Worker(ledger, composer, slack)
    assert worker.step("ledger_outbox", kinds=["kudos_submit"])
    assert store.get("ledger_outbox", key)["submission_result"]["status"] == "rejected"
    assert not store.get("ledger_evidence", "kudos:async-send")
    ui.action({"user": {"id": "U1"}, "trigger_id": "T",
               "actions": [{"action_id": "kudos_retry", "value": key}]}, slack)
    view = slack.views_open.call_args.kwargs["view"]
    assert view["callback_id"] == "kudos_recipient"
    meta = json.loads(view["private_metadata"])
    draft = ui.load_draft(str(oid(1)), meta["draft"])
    assert draft["message"] == "*Thanks* :hammer:"
    assert draft["public"] is True and draft["emoji"] == "hammer"
    assert "invitation" not in draft and meta["key"] != "async-send"
    api.complete.assert_not_called()


def test_consent_race_requires_review_with_authored_values_preserved(joined):
    ledger, store, _, composer, _, slack = joined
    ui = SlackUI(ledger, composer)
    key = kudos_submission.reserve(ui, modal_request(ledger, public=True))
    ledger.leave(str(oid(2)))
    worker = Worker(ledger, composer, slack)
    assert worker.step("ledger_outbox", kinds=["kudos_submit"])
    assert store.get("ledger_outbox", key)["submission_result"]["status"] == "review"
    assert not store.get("ledger_evidence", "kudos:async-send")
    kudos_submission.review(ui, {"user": {"id": "U1"}, "trigger_id": "T"}, slack, key)
    view = slack.views_open.call_args.kwargs["view"]
    assert "invitation" in json.dumps(view) and "will not earn XP" in json.dumps(view)
    assert "*Thanks* :hammer:" in json.dumps(view)
    assert json.loads(view["private_metadata"])["key"] != "async-send"


def test_transient_processing_failure_retries_without_early_failure_notice(joined):
    ledger, store, _, composer, _, slack = joined
    key = kudos_submission.reserve(SlackUI(ledger, composer), modal_request(ledger))
    worker = Worker(ledger, composer, slack)
    with patch.object(SlackUI, "confirm_human", side_effect=OSError("Slack unavailable")):
        assert worker.step("ledger_outbox", kinds=["kudos_submit"])
    row = store.get("ledger_outbox", key)
    assert row["status"] == "pending" and not row.get("submission_result")
    row["available_at"] = now()
    store.put("ledger_outbox", row)
    assert worker.step("ledger_outbox", kinds=["kudos_submit"])
    assert store.get("ledger_outbox", key)["submission_result"]["status"] == "accepted"


def test_terminal_processing_failure_has_fixed_retry_notice(joined):
    ledger, store, _, composer, _, slack = joined
    key = kudos_submission.reserve(SlackUI(ledger, composer), modal_request(ledger))
    row = store.get("ledger_outbox", key)
    row["attempts"] = 9
    store.put("ledger_outbox", row)
    worker = Worker(ledger, composer, slack)
    with patch.object(SlackUI, "confirm_human", side_effect=OSError("Slack unavailable")):
        assert worker.step("ledger_outbox", kinds=["kudos_submit"])
    assert store.get("ledger_outbox", key)["status"] == "failed"
    assert store.get("ledger_outbox", key)["submission_result"]["status"] == "retry"
    assert worker.step("ledger_outbox", kinds=["kudos_submission_reply"])
    assert "could not finish validating" in slack.chat_postMessage.call_args.kwargs["text"]


@pytest.mark.parametrize("terminal", [False, True])
def test_lost_response_after_committed_send_never_reports_failure_or_awards_twice(joined, terminal):
    ledger, store, _, composer, _, slack = joined
    key = kudos_submission.reserve(SlackUI(ledger, composer), modal_request(ledger))
    row = store.get("ledger_outbox", key)
    row["attempts"] = 9 if terminal else 0
    store.put("ledger_outbox", row)
    send = SlackUI.send_kudos
    def commit_then_disconnect(*args, **kwargs):
        send(*args, **kwargs)
        raise OSError("Lost response after commit")
    worker = Worker(ledger, composer, slack)
    with patch.object(SlackUI, "send_kudos", side_effect=commit_then_disconnect, autospec=True):
        assert worker.step("ledger_outbox", kinds=["kudos_submit"])
    evidence = store.get("ledger_evidence", "kudos:async-send")
    assert evidence
    if not terminal:
        row = store.get("ledger_outbox", key)
        row["available_at"] = now()
        store.put("ledger_outbox", row)
        with patch.object(SlackUI, "confirm_human", side_effect=AssertionError("Committed send was revalidated")):
            assert worker.step("ledger_outbox", kinds=["kudos_submit"])
    assert store.get("ledger_outbox", key)["submission_result"]["status"] == "accepted"
    assert len(store.select("ledger_evidence", {"kind": "kudos"})) == 1
    assert store.get("ledger_evidence", "kudos:async-send") == evidence
    assert worker.step("ledger_outbox", kinds=["kudos_submission_reply"])
    assert "saved and queued" in slack.chat_postMessage.call_args.kwargs["text"]


@pytest.mark.parametrize("status", ["activeMember", "pending", "expired", "inactive", "nonMember"])
@pytest.mark.parametrize("participating", [True, False])
def test_pending_and_lapsed_recipients_can_receive_kudos_and_requested_invitation(joined, status, participating):
    ledger, store, source, composer, _, slack = joined
    recipient = str(oid(2))
    if not participating:
        ledger.leave(recipient)
    source.data["members"][1].update(status=status, expirationTime=1)
    ui = SlackUI(ledger, composer)
    ui.command({"user_id": "U1", "command": "/kudos", "text": "<@U2|maker>", "trigger_id": "T"}, slack)
    assert slack.views_open.call_args.kwargs["view"]["callback_id"] == "kudos_send"
    choices = ui.options({"user_id": "U1", "action_id": "recipient", "value": "Maker2",
        "view": {"callback_id": "kudos_recipient"}})["options"]
    assert any(choice["value"] == recipient for choice in choices)
    key = kudos_submission.reserve(ui, modal_request(ledger, invitation="yes"))
    worker = Worker(ledger, composer, slack)
    assert worker.step("ledger_outbox", kinds=["kudos_submit"])
    assert store.get("ledger_outbox", key)["submission_result"]["status"] == "accepted"
    evidence = store.get("ledger_evidence", "kudos:async-send")
    assert evidence["xp_awarded"] == participating
    if not participating:
        assert store.get("ledger_relationships", "sponsor:" + recipient)
    assert worker.step("ledger_outbox", kinds=["kudos"])
    assert slack.chat_postMessage.call_args.kwargs["channel"] == "DU2"
    assert store.get("ledger_outbox", "kudos:async-send:recipient")["status"] == "done"
