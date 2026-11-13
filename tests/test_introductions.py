"""Consent and external side-effect boundaries for once-ever introductions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.slack_response import SlackResponse

from conftest import oid
from ledger import introductions as intro, views
from ledger.cli import outbox_filters
from ledger.domain import Denied
from ledger.slack_app import SlackUI
from ledger.storage import now
from ledger.worker import Worker, ingest_mqtt
from test_slack import form

MID = str(oid(1))


@pytest.fixture
def setup(env, monkeypatch):
    ledger, store, sources, composer, api, slack = env
    clock = [now()]
    monkeypatch.setattr(intro, "now", lambda: clock[0])
    monkeypatch.setattr("ledger.storage.now", lambda: clock[0])
    monkeypatch.setenv("LEDGER_AUTO_INTRODUCTIONS_ENABLED", "true")
    monkeypatch.delenv("LEDGER_FACILITIES_SHOP_ID", raising=False)
    sources.data["shops"].append({"_id": oid(299), "name": " Facilities "})
    sources.data["tools"].append({"_id": oid(399), "shop_id": oid(299), "name": "Orientation"})
    slack.conversations_history.return_value = {"messages": [], "response_metadata": {}}
    def post(**kwargs):
        stamp = f"{clock[0].timestamp():.6f}"
        clock[0] += timedelta(seconds=1)
        return {"ok": True, "ts": stamp}
    slack.chat_postMessage.side_effect = post
    worker = Worker(ledger, composer, slack, bot_id="UBOT")
    return ledger, store, sources, composer, api, slack, worker, clock


def baseline(setup):
    intro.scan(setup[0], {"_id": "baseline", "payload": {}})
    assert setup[1].get("ledger_catalog", intro.CONTROL)["phase"] == "active"
    setup[-1][0] += timedelta(seconds=1)


def checkout(setup, tool=311, *, member=1, date=True, revoked=False, identifier=501):
    row = {"_id": oid(identifier), "member_id": oid(member), "tool_id": oid(tool)}
    if date:
        row["checked_out_at"] = setup[-1][0]
    if revoked:
        row["revoked_at"] = setup[-1][0]
    setup[2].data["tool_checkouts"].append(row)
    return row


def drain(setup, kind):
    for _ in range(100):
        if not setup[6].step("ledger_outbox", kinds=[kind]):
            return
    pytest.fail("Unexpected introduction job loop")


def question(setup):
    baseline(setup)
    checkout(setup)
    intro.observe(setup[0], MID)
    drain(setup, "intro_send")
    assert intro.record(setup[1], MID)["status"] == "question"


def reply(setup, text, *, thread=None, user="U1", channel="DU1", subtype=None):
    event = {"type": "message", "user": user, "channel": channel, "channel_type": "im",
             "ts": str(setup[-1][0].timestamp()), "text": text}
    if thread:
        event["thread_ts"] = thread
    if subtype:
        event["subtype"] = subtype
    return setup[6].event(event, "slack:test")


def click(setup, action="intro_yes", slot="question", user="U1"):
    row = intro.record(setup[1], MID)
    return SlackUI(setup[0], setup[3]).action({"user": {"id": user}, "trigger_id": "trigger",
        "channel": {"id": "DU1"}, "message": {"ts": row["messages"][slot]["ts"]},
        "actions": [{"action_id": action, "value": row["_id"]}]}, setup[5])


def test_baseline_is_silent_and_future_checkout_is_once_ever(setup):
    checkout(setup, member=2)
    baseline(setup)
    assert not setup[1].select("ledger_outbox", {"kind": "intro_send"})
    assert intro.record(setup[1], str(oid(2)))["ever_nonfacilities"]
    checkout(setup)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: intro.observe(setup[0], MID), range(8)))
    drain(setup, "intro_send")
    assert setup[5].chat_postMessage.call_count == 1
    assert not setup[0].participant(MID)
    assert not setup[1].select("ledger_evidence", {"kind": "consent"})
    assert not setup[1].select("ledger_relationships", {"kind": "sponsor"})
    reply(setup, "No thanks")
    drain(setup, "intro_maintain")
    assert setup[5].chat_delete.call_count == 1
    setup[2].data["members"][0]["status"] = "expired"
    intro.observe(setup[0], MID)
    setup[2].data["members"][0]["status"] = "activeMember"
    checkout(setup, tool=321, identifier=502)
    intro.observe(setup[0], MID)
    drain(setup, "intro_send")
    assert setup[5].chat_postMessage.call_count == 1
    assert intro.record(setup[1], MID)["reserved_at"]
    setup[4].complete.assert_not_called()


def test_renewal_requires_no_checkout_in_attached_plan(setup):
    setup[2].data["members"][0]["status"] = "expired"
    baseline(setup)
    setup[2].data["members"][0]["status"] = "activeMember"
    intro.observe(setup[0], MID)
    drain(setup, "intro_send")
    assert intro.record(setup[1], MID)["trigger"] == "renewal"
    assert setup[5].chat_postMessage.call_count == 1


def test_facilities_checkout_does_not_consume_first_nonfacilities(setup):
    baseline(setup)
    checkout(setup, tool=399)
    intro.observe(setup[0], MID)
    assert not intro.record(setup[1], MID).get("reserved_at")
    checkout(setup, tool=311, identifier=502)
    intro.observe(setup[0], MID)
    drain(setup, "intro_send")
    assert setup[5].chat_postMessage.call_count == 1


@pytest.mark.parametrize("case", ["historical", "missing_date", "revoked_history", "unknown_tool"])
def test_unproven_or_old_first_checkout_never_triggers(setup, case):
    baseline(setup)
    item = checkout(setup, date=case != "missing_date", revoked=case == "revoked_history")
    if case in ("historical", "revoked_history"):
        item["checked_out_at"] -= timedelta(days=30)
    if case == "unknown_tool":
        item["tool_id"] = oid(99999)
    intro.observe(setup[0], MID)
    item.pop("revoked_at", None)
    checkout(setup, tool=321, identifier=502)
    intro.observe(setup[0], MID)
    drain(setup, "intro_send")
    setup[5].chat_postMessage.assert_not_called()


@pytest.mark.parametrize("case", ["blacklist", "pending", "expired", "suspended", "revoked", "merged", "ambiguous", "bot", "deleted", "former", "joined"])
def test_ineligible_members_are_never_introduced(setup, monkeypatch, case):
    baseline(setup)
    if case == "blacklist":
        monkeypatch.setenv("SLACKIDS_INELIGIBLE", '["U1"]')
    elif case in ("pending", "expired", "suspended", "revoked"):
        setup[2].data["members"][0]["status"] = case
    elif case == "merged":
        setup[2].data["members"][0]["merged_at"] = setup[-1][0]
    elif case == "ambiguous":
        setup[2].data["slack_users"].append({"_id": oid(999), "member_id": oid(2), "slack_id": "U1"})
    elif case in ("former", "joined"):
        setup[0].join(MID)
        if case == "former":
            setup[0].leave(MID)
    else:
        setup[5].users_info.side_effect = lambda user: {"user": {"id": user, "is_bot": case == "bot", "deleted": case == "deleted"}}
    checkout(setup)
    intro.observe(setup[0], MID)
    drain(setup, "intro_send")
    setup[5].chat_postMessage.assert_not_called()


def test_new_member_first_seen_after_activation_can_qualify(setup):
    baseline(setup)
    setup[2].data["members"].append({"_id": oid(20), "status": "activeMember"})
    setup[2].data["slack_users"].append({"_id": oid(120), "member_id": oid(20), "slack_id": "U20"})
    checkout(setup, member=20)
    intro.observe(setup[0], str(oid(20)))
    drain(setup, "intro_send")
    assert intro.record(setup[1], str(oid(20)))["status"] == "question"


def test_facilities_resolution_is_required_and_stable(setup, monkeypatch):
    setup[2].data["shops"].append({"_id": oid(298), "name": "FACILITIES"})
    with pytest.raises(ValueError, match="unambiguous"):
        baseline(setup)
    assert not setup[1].get("ledger_catalog", intro.CONTROL)
    monkeypatch.setenv("LEDGER_FACILITIES_SHOP_ID", str(oid(299)))
    baseline(setup)
    setup[2].data["shops"][-2]["name"] = "Orientation"
    assert setup[1].get("ledger_catalog", intro.CONTROL)["facilities_id"] == str(oid(299))


def test_baseline_pages_resume_without_sending(setup, monkeypatch):
    monkeypatch.setattr(intro, "PAGE_SIZE", 3)
    intro.scan(setup[0], {"_id": "baseline", "payload": {}})
    assert setup[1].get("ledger_catalog", intro.CONTROL)["phase"] == "baseline"
    while setup[6].step("ledger_inbox", kinds=["intro_scan"]):
        pass
    assert setup[1].get("ledger_catalog", intro.CONTROL)["phase"] == "active"
    assert len(setup[1].select(intro.COLLECTION, {"kind": "automatic_introduction"})) == 11
    setup[5].chat_postMessage.assert_not_called()


def test_yes_button_opens_explicit_consent_and_closing_does_not_extend(setup):
    question(setup)
    click(setup)
    modal = setup[5].views_open.call_args.kwargs["view"]
    assert intro.EXPLANATION in json.dumps(modal)
    assert "notify_on_close" not in modal
    assert not setup[0].participant(MID)
    original = intro.record(setup[1], MID)["deadline"]
    setup[-1][0] += timedelta(days=3)
    click(setup)
    assert intro.record(setup[1], MID)["deadline"] == original
    drain(setup, "intro_send")
    assert intro.record(setup[1], MID)["deadline"] == original
    with pytest.raises(ValueError, match="Explicitly"):
        SlackUI(setup[0], setup[3]).submission(form(modal, {"agree": False}), setup[5])
    result = SlackUI(setup[0], setup[3]).submission(form(modal, {"agree": True}), setup[5])
    assert result["response_action"] == "update"
    assert setup[0].participant(MID)["opted_in"]
    assert intro.record(setup[1], MID)["status"] == "joined"
    drain(setup, "intro_maintain")
    assert setup[5].chat_update.call_count == 2
    assert all(len(c.kwargs["blocks"]) == 1 for c in setup[5].chat_update.call_args_list)


def test_typed_yes_has_button_without_trying_to_open_modal(setup):
    question(setup)
    assert reply(setup, "Yes, tell me more!") == "introduction_handled"
    assert not setup[5].views_open.called
    drain(setup, "intro_send")
    message = setup[5].chat_postMessage.call_args.kwargs
    assert message["text"] == intro.EXPLANATION
    assert "intro_open" in json.dumps(message["blocks"])
    assert not setup[0].participant(MID)
    assert not setup[1].select("ledger_outbox", {"kind": "conversation"})
    assert not setup[1].select("ledger_context", {"kind": "message"})
    assert intro.record(setup[1], MID)["status"] == "consent"


@pytest.mark.parametrize("text,expected", [("Yes", "yes"), ("I love RPGs!", "yes"),
    ("Yes, please!", "yes"), ("No, thanks.", "no"), ("what's an RPG?", "unclear"),
    ("No thanks", "no"), ("I don't like role-playing games", "no"), ("not sure", "unclear"),
    ("yes, but I don't want to join", "unclear"), ("no, actually yes", "unclear"),
    ("what do you mean?", "unclear"), ("Where is the wood shop?", None)])
def test_conservative_reply_classification(text, expected):
    assert intro.classify(text) == expected


def test_clarification_is_once_only_and_never_extends_deadline(setup):
    question(setup)
    deadline = intro.record(setup[1], MID)["deadline"]
    reply(setup, "what do you mean?")
    drain(setup, "intro_send")
    reply(setup, "maybe")
    drain(setup, "intro_send")
    assert setup[5].chat_postMessage.call_count == 2
    assert setup[5].chat_postMessage.call_args.kwargs["text"] == intro.CLARIFICATION
    assert intro.record(setup[1], MID)["deadline"] == deadline


@pytest.mark.parametrize("stage", ["question", "consent"])
def test_timeout_deletes_only_owned_intro_and_rejects_late_actions(setup, stage):
    question(setup)
    if stage == "consent":
        reply(setup, "yes")
        drain(setup, "intro_send")
    row = intro.record(setup[1], MID)
    setup[-1][0] = row["deadline"]
    # Cleanup is in the independent channels lane and works while paused.
    setup[1].put("ledger_catalog", {"_id": "control", "paused": True})
    assert "intro_maintain" in outbox_filters("channels")["kinds"]
    drain(setup, "intro_maintain")
    assert intro.record(setup[1], MID)["status"] == "expired"
    expected = 2 if stage == "consent" else 1
    assert setup[5].chat_delete.call_count == expected
    assert {c.kwargs["ts"] for c in setup[5].chat_delete.call_args_list} == {m["ts"] for m in row["messages"].values()}
    click(setup)
    setup[5].views_open.assert_not_called()
    with pytest.raises(Denied):
        setup[0].join(MID, introduction=row["_id"])
    assert not setup[0].participant(MID)


def test_decline_does_not_prevent_later_peer_invitation(setup):
    question(setup)
    reply(setup, "no")
    drain(setup, "intro_maintain")
    giver = str(oid(2))
    setup[0].join(giver)
    setup[0].sponsor(giver, MID)
    setup[0].join(MID, giver)
    assert setup[0].participant(MID)["opted_in"]
    assert setup[1].get("ledger_relationships", "sponsor:" + MID)["status"] == "accepted"


def test_join_elsewhere_cancels_pending_explanation_transactionally(setup):
    question(setup)
    reply(setup, "yes")
    setup[0].join(MID)
    drain(setup, "intro_send")
    assert setup[5].chat_postMessage.call_count == 1
    assert intro.record(setup[1], MID)["status"] == "joined"
    assert setup[0].participant(MID)["import_pending"]


def test_unrelated_thread_other_user_edits_and_old_replies_do_not_answer(setup):
    question(setup)
    for changes in ({"thread": "111.222"}, {"user": "U2"}, {"channel": "DOTHER"}, {"subtype": "message_changed"}):
        # Test the introduction router directly to avoid unrelated generic chat behavior.
        row = {"type": "message", "text": "yes", "ts": str(setup[-1][0].timestamp()), "user": changes.get("user", "U1"),
               "channel": changes.get("channel", "DU1"), "thread_ts": changes.get("thread"), "subtype": changes.get("subtype")}
        assert not intro.event(setup[6], row)
    assert intro.event(setup[6], {"type": "message", "text": "yes", "ts": "1", "user": "U1", "channel": "DU1"})
    assert intro.record(setup[1], MID)["status"] == "question"


def test_uncertain_post_never_reposts_and_recovers_receipt_for_cleanup(setup):
    baseline(setup)
    checkout(setup)
    intro.observe(setup[0], MID)
    accepted_at = str(setup[-1][0].timestamp())
    setup[5].chat_postMessage.side_effect = TimeoutError("remote acceptance unknown")
    drain(setup, "intro_send")
    row = intro.record(setup[1], MID)
    assert row["messages"]["question"]["state"] == "unknown"
    # Simulate a reclaimed job after process death.
    job = setup[1].get("ledger_outbox", row["_id"] + ":question")
    job.update(status="pending", available_at=setup[-1][0])
    setup[1].put("ledger_outbox", job)
    drain(setup, "intro_send")
    assert setup[5].chat_postMessage.call_count == 1
    setup[5].conversations_history.return_value = {"messages": [{"ts": accepted_at, "user": "UBOT",
        "client_msg_id": row["messages"]["question"]["client_msg_id"]}]}
    setup[-1][0] += timedelta(days=15)
    intro.maintain(setup[6], MID)
    assert intro.record(setup[1], MID)["status"] == "expired"
    setup[5].chat_delete.assert_called_once_with(channel="DU1", ts=accepted_at)


def test_late_bot_receipt_after_suppression_is_deleted(setup, monkeypatch):
    baseline(setup)
    checkout(setup)
    intro.observe(setup[0], MID)
    setup[5].chat_postMessage.side_effect = TimeoutError()
    drain(setup, "intro_send")
    monkeypatch.setenv("SLACKIDS_INELIGIBLE", '["U1"]')
    intro.maintain(setup[6], MID)
    row = intro.record(setup[1], MID)
    assert row["status"] == "suppressed"
    event = {"type": "message", "user": "UBOT", "channel": "DU1", "ts": str(setup[-1][0].timestamp()),
        "client_msg_id": row["messages"]["question"]["client_msg_id"],
        "metadata": {"event_type": "ledger_introduction", "event_payload": {"member_id": MID, "slot": "question"}}}
    assert intro.event(setup[6], event)
    intro.maintain(setup[6], MID)
    assert setup[5].chat_delete.call_count == 1


def test_explicit_rejection_can_retry_and_deletion_failure_remains_pending(setup):
    baseline(setup)
    checkout(setup)
    intro.observe(setup[0], MID)
    setup[5].chat_postMessage.side_effect = SlackApiError("rejected", SlackResponse(client=None, http_verb="POST",
        api_url="https://slack.com/api/chat.postMessage", req_args={}, data={"error": "missing_scope"}, headers={}, status_code=200))
    drain(setup, "intro_send")
    row = intro.record(setup[1], MID)
    assert row["messages"]["question"]["state"] == "ready"
    setup[-1][0] += timedelta(seconds=5)
    setup[5].chat_postMessage.side_effect = lambda **_: {"ts": str(setup[-1][0].timestamp())}
    drain(setup, "intro_send")
    setup[-1][0] += timedelta(seconds=1)
    reply(setup, "no")
    setup[5].chat_delete.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        intro.maintain(setup[6], MID)
    assert not intro.record(setup[1], MID)["messages"]["question"].get("cleaned_at")
    setup[5].chat_delete.side_effect = SlackApiError("gone", {"error": "message_not_found"})
    intro.maintain(setup[6], MID)
    assert intro.record(setup[1], MID)["messages"]["question"]["cleaned_at"]


def test_remapping_cancels_without_sending_to_new_identity(setup):
    question(setup)
    setup[2].data["slack_users"][0]["slack_id"] = "UNEW"
    intro.maintain(setup[6], MID)
    assert intro.record(setup[1], MID)["status"] == "suppressed"
    assert setup[5].chat_delete.call_args.kwargs["channel"] == "DU1"
    intro.observe(setup[0], MID)
    drain(setup, "intro_send")
    assert setup[5].chat_postMessage.call_count == 1


def test_source_failure_does_not_prevent_expired_cleanup(setup, monkeypatch):
    question(setup)
    setup[-1][0] += timedelta(days=15)
    monkeypatch.setattr(setup[2], "member", lambda _: (_ for _ in ()).throw(RuntimeError("source offline")))
    intro.maintain(setup[6], MID)
    setup[5].chat_delete.assert_called_once()


def test_disable_and_reenable_baselines_changes_without_catchup(setup, monkeypatch):
    baseline(setup)
    monkeypatch.setenv("LEDGER_AUTO_INTRODUCTIONS_ENABLED", "false")
    intro.schedule_scan(setup[0], "disabled")
    checkout(setup)
    monkeypatch.setenv("LEDGER_AUTO_INTRODUCTIONS_ENABLED", "true")
    baseline(setup)
    intro.observe(setup[0], MID)
    drain(setup, "intro_send")
    setup[5].chat_postMessage.assert_not_called()


def test_mqtt_observes_nonparticipants_from_authoritative_source(setup):
    from bson import json_util
    baseline(setup)
    setup[2].data["members"][0]["status"] = "expired"
    def packet(status):
        return ("update 123 " + json_util.dumps({"document": {"_id": oid(1), "status": status}})).encode()
    ingest_mqtt(setup[1], "members/update", packet("expired"))
    while setup[6].step("ledger_inbox", kinds=["intro_observe"]):
        pass
    setup[2].data["members"][0]["status"] = "activeMember"
    # Packet contents cannot authorize the send: the source is re-read.
    ingest_mqtt(setup[1], "members/update", packet("pending"))
    while setup[6].step("ledger_inbox", kinds=["intro_observe"]):
        pass
    drain(setup, "intro_send")
    assert setup[5].chat_postMessage.call_count == 1


def test_stale_source_snapshot_is_refreshed_before_commit(setup, monkeypatch):
    baseline(setup)
    original = setup[2].tools_by_id
    changed = [False]
    def tools(*args, **kwargs):
        if not changed[0]:
            changed[0] = True
            setup[2].data["members"][0]["status"] = "activeMember"
            intro.observe(setup[0], MID)
        return original(*args, **kwargs)
    setup[2].data["members"][0]["status"] = "expired"
    monkeypatch.setattr(setup[2], "tools_by_id", tools)
    intro.observe(setup[0], MID)
    row = intro.record(setup[1], MID)
    assert row["observed_status"] == "activeMember"
    assert not row.get("reserved_at")


def test_join_during_remote_post_does_not_reopen_introduction(setup):
    baseline(setup)
    checkout(setup)
    intro.observe(setup[0], MID)
    def post(**kwargs):
        setup[0].join(MID)
        return {"ts": str(setup[-1][0].timestamp())}
    setup[5].chat_postMessage.side_effect = post
    drain(setup, "intro_send")
    assert intro.record(setup[1], MID)["status"] == "joined"
    drain(setup, "intro_maintain")
    setup[5].chat_update.assert_called_once()
    assert not setup[1].select("ledger_outbox", {"kind": "intro_send", "payload.slot": "explanation"})


def test_original_question_deadline_cannot_expire_new_consent_stage(setup):
    question(setup)
    original_deadline = intro.record(setup[1], MID)["deadline"]
    setup[-1][0] += timedelta(days=10)
    reply(setup, "yes")
    drain(setup, "intro_send")
    second_deadline = intro.record(setup[1], MID)["deadline"]
    setup[-1][0] = original_deadline
    drain(setup, "intro_maintain")
    assert intro.record(setup[1], MID)["status"] == "consent"
    assert intro.record(setup[1], MID)["deadline"] == second_deadline
    setup[5].chat_delete.assert_not_called()


def test_multiple_reconciliations_converge_on_one_poll_bucket(setup):
    question(setup)
    bucket = (int(setup[-1][0].timestamp()) // 900 + 1) * 900
    setup[-1][0] = datetime.fromtimestamp(bucket + 1, timezone.utc)
    for _ in range(10):
        intro.observe(setup[0], MID)
        intro.maintain(setup[6], MID)
        setup[-1][0] += timedelta(seconds=30)
    polls = setup[1].select("ledger_outbox", {"kind": "intro_maintain"})
    assert sum(":poll:" in job["_id"] and job["available_at"] > setup[-1][0] for job in polls) == 1


def test_operational_counts_expose_uncertainty_and_cleanup_without_identity(setup):
    baseline(setup)
    checkout(setup)
    intro.observe(setup[0], MID)
    setup[5].chat_postMessage.side_effect = TimeoutError()
    drain(setup, "intro_send")
    counts = intro.diagnostics(setup[1])
    assert counts["uncertain_deliveries"] == 1
    assert counts["states"]["queued"] == 1
    assert MID not in json.dumps(counts) and "U1" not in json.dumps(counts)
    row = intro.record(setup[1], MID)
    intro.receipt(setup[0], MID, "question", "DU1", str(setup[-1][0].timestamp()))
    setup[-1][0] += timedelta(seconds=1)
    reply(setup, "no")
    assert intro.diagnostics(setup[1])["cleanup_pending"] == 1
    intro.maintain(setup[6], MID)
    assert intro.diagnostics(setup[1])["cleanup_pending"] == 0


def test_source_merge_cannot_repeat_intro_for_same_slack_identity(setup):
    question(setup)
    reply(setup, "no")
    setup[2].data["members"][0]["merged_at"] = setup[-1][0]
    setup[2].data["members"].append({"_id": oid(20), "status": "activeMember"})
    setup[2].data["slack_users"][0]["member_id"] = oid(20)
    checkout(setup, member=20, identifier=502)
    intro.observe(setup[0], str(oid(20)))
    drain(setup, "intro_send")
    assert setup[5].chat_postMessage.call_count == 1
