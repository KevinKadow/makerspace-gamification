import json

import pytest

from ledger.domain import Denied
from ledger import views
from ledger.ineligible import is_ineligible_slack_id, parse_slackids_ineligible
from ledger.slack_app import SlackUI
from ledger.worker import Worker

from conftest import oid
from test_slack import form


def test_parse_ineligible_slack_id_set_forms(monkeypatch):
    value = '{"U0123456789","U0345678901"}'
    assert parse_slackids_ineligible(value) == {"U0123456789", "U0345678901"}
    assert parse_slackids_ineligible('["U0123456789"]') == {"U0123456789"}
    monkeypatch.setenv("SLACKIDS_INELIGIBLE", value)
    assert is_ineligible_slack_id("U0123456789")
    assert not is_ineligible_slack_id("U999")


@pytest.mark.parametrize("value", ["{U1}", '["bad"]', "not-a-list"])
def test_parse_rejects_malformed_or_invalid_ids(value):
    with pytest.raises(ValueError):
        parse_slackids_ineligible(value)


def test_excluded_identity_cannot_opt_in_or_receive_sponsorship(joined, monkeypatch):
    ledger, store, _, _, _, _ = joined
    member_id = str(oid(3))
    monkeypatch.setenv("SLACKIDS_INELIGIBLE", '["U3"]')
    assert not ledger.member_eligible(member_id)
    assert not ledger.active(member_id)
    with pytest.raises(Denied):
        ledger.join(member_id)
    assert ledger.sponsor(str(oid(1)), member_id) is None
    assert store.get("ledger_relationships", f"sponsor:{member_id}") is None


def test_excluded_user_commands_are_silent_except_kudos(joined, monkeypatch):
    ledger, _, _, composer, _, slack = joined
    monkeypatch.setenv("SLACKIDS_INELIGIBLE", '["U1"]')
    ui = SlackUI(ledger, composer)
    assert ui.command({"user_id": "U1", "command": "/ledger", "text": "progress"}, slack) is None
    slack.views_open.assert_not_called()
    # The normal public /kudos flow is still allowed to open its recipient picker.
    ui.command({"user_id": "U1", "command": "/kudos", "text": "", "trigger_id": "trigger"}, slack)
    slack.views_open.assert_called_once()


@pytest.mark.parametrize("action_id", ["kudos_change", "shop"])
def test_excluded_kudos_sender_can_use_kudos_form_actions(joined, monkeypatch, action_id):
    ledger, _, _, composer, _, slack = joined
    monkeypatch.setenv("SLACKIDS_INELIGIBLE", '["U1"]')
    ui = SlackUI(ledger, composer)
    view = views.kudos_form(ledger, str(oid(2)), "excluded-kudos", {"message": "Thanks"})
    body = form(view, {"message": "Thanks", "shop": str(oid(201))}, user="U1")
    body["actions"] = [{"action_id": action_id, "value": "selected"}]
    ui.action(body, slack)
    updated = slack.views_update.call_args.kwargs["view"]
    if action_id == "kudos_change":
        assert updated["callback_id"] == "kudos_recipient"
        assert updated["blocks"][0]["element"]["action_id"] == "recipient"
    else:
        assert updated["callback_id"] == "kudos_send"
        assert any(block.get("block_id", "").startswith("tool:") for block in updated["blocks"])


def test_kudos_to_excluded_identity_delivers_but_suppresses_invitation(joined, monkeypatch):
    ledger, store, _, composer, _, slack = joined
    giver, recipient = str(oid(1)), str(oid(3))
    monkeypatch.setenv("SLACKIDS_INELIGIBLE", '["U3"]')
    evidence = ledger.kudos(giver, recipient, "Nice repair work", key="blocked-target",
        invite=True, expected_participation=False)
    assert evidence["invite"] is False
    assert store.get("ledger_relationships", f"sponsor:{recipient}") is None
    job = store.get("ledger_outbox", evidence["_id"] + ":recipient")
    job.update(status="working", lease="kudos-lease")
    store.put("ledger_outbox", job)
    Worker(ledger, composer, slack).deliver_kudos(job)
    assert slack.chat_postMessage.call_args.kwargs["channel"] == "DU3"
    assert "actions" not in json.dumps(slack.chat_postMessage.call_args.kwargs.get("blocks", []))


def test_excluded_kudos_sender_cannot_invite(joined, monkeypatch):
    ledger, store, _, _, _, _ = joined
    giver, recipient = str(oid(1)), str(oid(3))
    monkeypatch.setenv("SLACKIDS_INELIGIBLE", '["U1"]')
    evidence = ledger.kudos(giver, recipient, "Thanks", key="blocked-giver",
        invite=True, expected_participation=False)
    assert evidence["invite"] is False
    assert store.get("ledger_relationships", f"sponsor:{recipient}") is None
    assert evidence.get("summary_id") is None


def test_channel_join_by_excluded_identity_is_removed(joined, monkeypatch):
    ledger, store, _, composer, _, slack = joined
    monkeypatch.setenv("SLACKIDS_INELIGIBLE", '["U1"]')
    worker = Worker(ledger, composer, slack)
    worker.event({"type": "member_joined_channel", "channel": "CCHAT", "user": "U1"}, "blocked-join")
    jobs = store.select("ledger_outbox", {"kind": "remove", "payload.channel": "CCHAT"})
    assert jobs and jobs[0]["payload"]["slack_id"] == "U1"


def test_excluded_identity_cannot_receive_ordinary_ledger_dm(joined, monkeypatch):
    ledger, store, _, composer, _, slack = joined
    member_id = str(oid(1))
    monkeypatch.setenv("SLACKIDS_INELIGIBLE", '["U1"]')
    ledger.notify(member_id, "status", {"summary": "private"}, "blocked-dm", exception=True)
    worker = Worker(ledger, composer, slack)
    assert worker.valid_identity(member_id) is None
    assert store.get("ledger_outbox", "dm:blocked-dm") is None
