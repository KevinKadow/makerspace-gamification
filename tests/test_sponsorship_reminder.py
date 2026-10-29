import argparse
from copy import deepcopy
from datetime import timedelta
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pymongo.errors import PyMongoError
from slack_sdk.errors import SlackApiError

from ledger.domain import Denied
from ledger.sponsorship_reminder import (CATEGORIES, DEFAULT_VARIANTS, eligible_participants,
    load_variants, merge_reminder_receipt, parse_days, run)
from ledger.storage import now
from ledger.worker import Worker
from ledger.review_notifications import ReviewDeliveryBusy

from conftest import oid


def options(days, **kwargs):
    return argparse.Namespace(days=days, variants=None, dry_run=kwargs.get("dry_run", False),
        generate=kwargs.get("generate", False), verbose=kwargs.get("verbose", False),
        debug=kwargs.get("debug", False), limit=kwargs.get("limit"), member=kwargs.get("member"))


def save_receipt(ledger, store, member_id, **values):
    participant = ledger.participant(member_id)
    participant.update(reminder_ts="123.456", reminder_sent_at=now(), reminder_channel="D" + ledger.sources.slack_id(member_id),
        reminder_slack_id=ledger.sources.slack_id(member_id),
        reminder_consent_generation=participant.get("consent_generation"), **values)
    store.put("ledger_participants", participant)
    return participant


def test_days_select_never_and_stale_inviters(joined):
    ledger, store, *_ = joined
    giver, recipient = str(oid(1)), str(oid(3))
    ledger.sponsor(giver, recipient)
    row = store.get("ledger_relationships", f"sponsor-invite:{giver}:{recipient}")
    row["at"] = now() - timedelta(days=31)
    store.put("ledger_relationships", row)

    never = eligible_participants(ledger, "never")
    stale = eligible_participants(ledger, 30)
    assert [row["member_id"] for row in never] == [str(oid(2))]
    assert [row["member_id"] for row in stale] == [giver]
    assert parse_days("NEVER") == "never"
    assert parse_days("14") == 14


def test_dry_run_prints_prompt_without_writes_or_posts(joined):
    ledger, store, _, composer, _, slack = joined
    original = deepcopy(store.data)
    output = io.StringIO()
    totals = run(ledger, composer, slack, options("never", dry_run=True), stdout=output, stderr=io.StringIO())
    assert totals == {"sent": 0, "updated": 0, "failed": 0, "mongo_updated": 0}
    assert '"action": "send"' in output.getvalue()
    assert store.data == original
    slack.chat_postMessage.assert_not_called()
    slack.chat_update.assert_not_called()


def test_dry_run_generate_prints_text_without_writes(joined):
    ledger, store, _, composer, api, slack = joined
    original = deepcopy(store.data)
    api.complete.return_value = "Invite someone you know if they are interested."
    output = io.StringIO()
    run(ledger, composer, slack, options("never", dry_run=True, generate=True),
        stdout=output, stderr=io.StringIO())
    assert "generated_text" in output.getvalue()
    assert "qwen_prompt" not in output.getvalue()
    assert store.data == original
    slack.chat_postMessage.assert_not_called()


def test_dry_run_preview_avoids_the_previous_variant(joined):
    ledger, store, _, composer, _, slack = joined
    member_id = str(oid(1))
    template = load_variants()["reminder_never"]
    composer.choose = lambda choices: choices[0]
    participant = ledger.participant(member_id)
    participant.update(reminder_variant_index=0, reminder_variant_category="reminder_never")
    store.put("ledger_participants", participant)
    stdout = io.StringIO()
    run(ledger, composer, slack, options("never", dry_run=True, member=member_id),
        stdout=stdout, stderr=io.StringIO())
    prompt = json.loads(stdout.getvalue())["qwen_prompt"]
    assert "patient workshop mentor" in prompt[0]["content"]


def test_delivery_retry_reuses_saved_prompt_and_text_after_slack_failure(joined):
    ledger, store, _, composer, api, slack = joined
    member_id = str(oid(1))
    api.complete.return_value = "A saved reminder with one stable variation."
    slack.chat_postMessage.side_effect = TimeoutError("temporary failure")
    first = run(ledger, composer, slack, options("never", member=member_id),
        stdout=io.StringIO(), stderr=io.StringIO())
    pending = ledger.participant(member_id)
    pending_id = pending["reminder_pending_id"]
    selected_prompt = pending["reminder_pending_selection"]
    saved_text = pending["reminder_pending_text"]
    first_id = slack.chat_postMessage.call_args.kwargs["client_msg_id"]
    assert first["failed"] == 1 and saved_text == api.complete.return_value

    slack.chat_postMessage.side_effect = None
    second = run(ledger, composer, slack, options("never", member=member_id),
        stdout=io.StringIO(), stderr=io.StringIO())
    sent = slack.chat_postMessage.call_args.kwargs
    assert api.complete.call_count == 1
    assert sent["text"] == saved_text
    assert sent["client_msg_id"] == first_id
    assert ledger.participant(member_id)["reminder_pending_id"] is None
    assert second["sent"] == 1 and selected_prompt["template"]["variations"][0]["id"]
    assert pending_id


def test_receipt_write_retry_reuses_composition_after_slack_accepted_post(joined):
    ledger, store, _, composer, api, slack = joined
    member_id = str(oid(1))
    api.complete.return_value = "A reminder accepted by Slack once."
    fail_write = [False]
    original_put = store.put

    def post(**kwargs):
        fail_write[0] = True
        return {"ts": "accepted.1", "channel": kwargs["channel"]}

    def put(collection, document):
        if fail_write[0] and collection == "ledger_participants" and document.get("reminder_ts") == "accepted.1":
            fail_write[0] = False
            raise PyMongoError("receipt write failed")
        return original_put(collection, document)

    slack.chat_postMessage.side_effect = post
    with patch.object(store, "put", side_effect=put):
        first = run(ledger, composer, slack, options("never", member=member_id),
            stdout=io.StringIO(), stderr=io.StringIO())
    assert first["sent"] == 1 and first["mongo_updated"] == 0
    saved_text = ledger.participant(member_id)["reminder_pending_text"]
    first_id = slack.chat_postMessage.call_args.kwargs["client_msg_id"]

    slack.chat_postMessage.side_effect = None
    second = run(ledger, composer, slack, options("never", member=member_id),
        stdout=io.StringIO(), stderr=io.StringIO())
    assert api.complete.call_count == 1
    assert slack.chat_postMessage.call_args.kwargs["text"] == saved_text
    assert slack.chat_postMessage.call_args.kwargs["client_msg_id"] == first_id
    assert ledger.participant(member_id)["reminder_ts"] == "123.456"
    assert second["sent"] == 1


def test_new_reminder_saves_receipt_then_recent_run_updates_same_dm(joined):
    ledger, store, _, composer, _, slack = joined
    # Member 1 has previously invited someone, so target only their old invite history.
    giver, recipient = str(oid(1)), str(oid(3))
    ledger.sponsor(giver, recipient)
    row = store.get("ledger_relationships", f"sponsor-invite:{giver}:{recipient}")
    row["at"] = now() - timedelta(days=31)
    store.put("ledger_relationships", row)

    first = run(ledger, composer, slack, options(30), stdout=io.StringIO(), stderr=io.StringIO())
    receipt = ledger.participant(giver)
    sent_at = receipt["reminder_sent_at"]
    assert first["sent"] == 1 and first["mongo_updated"] == 1
    assert receipt["reminder_ts"] == "123.456"
    assert receipt["reminder_channel"] == "DU1"

    second = run(ledger, composer, slack, options(30), stdout=io.StringIO(), stderr=io.StringIO())
    assert second["updated"] == 1
    assert second["mongo_updated"] == 1
    assert ledger.participant(giver)["reminder_sent_at"] == sent_at
    assert ledger.participant(giver)["reminder_accepted_template"]
    assert slack.chat_update.call_args.kwargs["channel"] == "DU1"
    assert slack.chat_update.call_args.kwargs["ts"] == "123.456"


def test_initial_reminders_rotate_variant_indexes_and_save_usage_time(joined):
    ledger, _, _, composer, _, slack = joined
    member_id = str(oid(1))
    composer.choose = lambda choices: choices[0]
    run(ledger, composer, slack, options("never", member=member_id), stdout=io.StringIO(), stderr=io.StringIO())
    first = ledger.participant(member_id)
    first_index, first_at = first["reminder_variant_index"], first["reminder_variant_used_at"]

    run(ledger, composer, slack, options("never", member=member_id), stdout=io.StringIO(), stderr=io.StringIO())
    second = ledger.participant(member_id)
    assert second["reminder_variant_index"] != first_index
    assert second["reminder_variant_used_at"] >= first_at


def test_receipt_merge_preserves_concurrent_progress_and_opt_out(joined):
    ledger, store, _, composer, _, slack = joined
    member_id = str(oid(1))

    def post_message(**kwargs):
        participant = ledger.participant(member_id)
        participant.update(xp="77", opted_in=False, revision=participant["revision"] + 1,
                           consent_generation=participant["consent_generation"] + 1)
        store.put("ledger_participants", participant)
        return {"ts": "concurrent.1", "channel": kwargs["channel"]}

    slack.chat_postMessage.side_effect = post_message
    totals = run(ledger, composer, slack, options("never", member=member_id),
                 stdout=io.StringIO(), stderr=io.StringIO())
    current = ledger.participant(member_id)
    assert current["xp"] == "77" and current["opted_in"] is False
    assert "reminder_ts" not in current
    assert totals["sent"] == 1 and totals["mongo_updated"] == 0


def test_new_send_rechecks_opt_out_after_opening_dm(joined):
    ledger, store, _, composer, _, slack = joined
    member_id = str(oid(1))

    def open_after_opt_out(users):
        participant = ledger.participant(member_id)
        participant.update(opted_in=False, consent_generation=participant["consent_generation"] + 1)
        store.put("ledger_participants", participant)
        return {"channel": {"id": "D1"}}

    slack.conversations_open.side_effect = open_after_opt_out
    totals = run(ledger, composer, slack, options("never", member=member_id),
        stdout=io.StringIO(), stderr=io.StringIO())
    slack.chat_postMessage.assert_not_called()
    assert totals["sent"] == 0


def test_new_send_rechecks_linked_identity_after_opening_dm(joined):
    ledger, _, source, composer, _, slack = joined
    member_id = str(oid(1))

    def open_after_relink(users):
        source.data["slack_users"] = [
            {**row, "slack_id": "UNEW1"} if row["member_id"] == oid(1) else row
            for row in source.data["slack_users"]]
        return {"channel": {"id": "DU1"}}

    slack.conversations_open.side_effect = open_after_relink
    totals = run(ledger, composer, slack, options("never", member=member_id),
        stdout=io.StringIO(), stderr=io.StringIO())
    slack.chat_postMessage.assert_not_called()
    assert totals["sent"] == 0


@pytest.mark.parametrize("change", ["opt_out", "relink"])
def test_deleted_reminder_fallback_revalidates_recipient_after_open(joined, change):
    ledger, store, source, composer, _, slack = joined
    member_id = str(oid(1))
    save_receipt(ledger, store, member_id)
    slack.chat_update.side_effect = SlackApiError("message missing", {"error": "message_not_found"})

    def open_after_change(users):
        if change == "opt_out":
            participant = ledger.participant(member_id)
            participant.update(opted_in=False, consent_generation=participant["consent_generation"] + 1)
            store.put("ledger_participants", participant)
        else:
            source.data["slack_users"] = [
                {**row, "slack_id": "UNEW1"} if row["member_id"] == oid(1) else row
                for row in source.data["slack_users"]]
        return {"channel": {"id": "DU1"}}

    slack.conversations_open.side_effect = open_after_change
    totals = run(ledger, composer, slack, options("never", member=member_id),
        stdout=io.StringIO(), stderr=io.StringIO())
    slack.chat_postMessage.assert_not_called()
    assert totals["sent"] == 0


def test_receipt_merge_preserves_concurrent_progress_for_active_member(joined):
    ledger, store, _, composer, _, slack = joined
    member_id = str(oid(1))

    def post_message(**kwargs):
        participant = ledger.participant(member_id)
        participant.update(xp="77", revision=participant["revision"] + 1)
        store.put("ledger_participants", participant)
        return {"ts": "concurrent.2", "channel": kwargs["channel"]}

    slack.chat_postMessage.side_effect = post_message
    totals = run(ledger, composer, slack, options("never", member=member_id),
                 stdout=io.StringIO(), stderr=io.StringIO())
    current = ledger.participant(member_id)
    assert current["xp"] == "77" and current["reminder_ts"] == "concurrent.2"
    assert totals["sent"] == 1 and totals["mongo_updated"] == 1


def test_relinked_identity_gets_a_new_bound_dm_receipt(joined):
    ledger, store, source, composer, _, slack = joined
    member_id = str(oid(1))
    run(ledger, composer, slack, options("never", member=member_id), stdout=io.StringIO(), stderr=io.StringIO())
    assert ledger.participant(member_id)["reminder_slack_id"] == "U1"
    worker = Worker(ledger, composer, slack)
    assert worker.queue_sponsorship_reminder_followup(member_id, "invitation_sent", str(oid(3)))
    old_job = store.get("ledger_outbox", f"sponsorship-reminder-invited:{member_id}:{oid(3)}")

    source.data["slack_users"] = [
        {**row, "slack_id": "UNEW1"} if row["member_id"] == oid(1) else row
        for row in source.data["slack_users"]]
    slack.chat_update.reset_mock()
    slack.chat_postMessage.reset_mock()
    old_job.update(status="working", lease="old-identity")
    store.put("ledger_outbox", old_job)
    with pytest.raises(Denied, match="no longer current"):
        worker.deliver_sponsorship_reminder_followup(old_job)
    run(ledger, composer, slack, options("never", member=member_id), stdout=io.StringIO(), stderr=io.StringIO())

    current = ledger.participant(member_id)
    assert current["reminder_slack_id"] == "UNEW1" and current["reminder_channel"] == "DUNEW1"
    slack.chat_update.assert_not_called()
    assert slack.chat_postMessage.call_args.kwargs["channel"] == "DUNEW1"


def test_custom_variant_file_has_all_required_categories():
    assert set(load_variants()) == {"reminder_never", "reminder_previous", "invitation_accepted"}


def test_custom_variants_load_and_validate_every_category():
    document = json.loads(DEFAULT_VARIANTS.read_text(encoding="utf-8"))
    with patch("ledger.sponsorship_reminder.Path.read_text", return_value=json.dumps(document)):
        assert set(load_variants("custom-variants.json")) == set(CATEGORIES)
    document["invitation_accepted"]["variations"] = []
    with patch("ledger.sponsorship_reminder.Path.read_text", return_value=json.dumps(document)):
        with pytest.raises(ValueError):
            load_variants("custom-variants.json")


def test_invitation_acceptance_queues_and_delivers_recent_followup(joined):
    ledger, store, _, composer, _, slack = joined
    inviter, accepted = str(oid(1)), str(oid(3))
    save_receipt(ledger, store, inviter)
    ledger.sponsor(inviter, accepted)
    ledger.join(accepted, sponsor=inviter)

    job = store.get("ledger_outbox", f"sponsorship-reminder-accepted:{accepted}:{inviter}:1")
    assert job and job["kind"] == "sponsorship_reminder_followup"
    assert job["payload"]["member_id"] == inviter
    worker = Worker(ledger, composer, slack)
    assert worker.step("ledger_outbox", kinds=["sponsorship_reminder_followup"])
    text = slack.chat_update.call_args.kwargs["text"]
    assert "Maker3 Test" in text
    assert "<#CCHAT>" in text
    assert store.get("ledger_outbox", job["_id"])["status"] == "done"


def test_acceptance_followup_targets_the_inviter_whose_invitation_was_accepted(joined):
    ledger, store, _, _, _, _ = joined
    first_inviter, accepted_inviter, recipient = map(str, (oid(1), oid(2), oid(3)))
    save_receipt(ledger, store, first_inviter)
    save_receipt(ledger, store, accepted_inviter)
    ledger.sponsor(first_inviter, recipient)
    ledger.sponsor(accepted_inviter, recipient)
    ledger.join(recipient, sponsor=accepted_inviter)
    job = store.get("ledger_outbox", f"sponsorship-reminder-accepted:{recipient}:{accepted_inviter}:1")
    assert job["payload"]["member_id"] == accepted_inviter
    assert store.get("ledger_relationships", f"sponsor:{recipient}")["giver"] == first_inviter


def test_rejoin_through_second_sponsor_queues_distinct_acceptance_followup(joined):
    ledger, store, _, _, _, _ = joined
    first_inviter, second_inviter, recipient = map(str, (oid(1), oid(2), oid(3)))
    save_receipt(ledger, store, first_inviter)
    save_receipt(ledger, store, second_inviter)
    ledger.sponsor(first_inviter, recipient)
    ledger.join(recipient, sponsor=first_inviter)
    first_job_id = f"sponsorship-reminder-accepted:{recipient}:{first_inviter}:1"
    first_job = store.get("ledger_outbox", first_job_id)
    assert first_job

    ledger.leave(recipient)
    ledger.sponsor(second_inviter, recipient)
    ledger.join(recipient, sponsor=second_inviter)

    second_job_id = f"sponsorship-reminder-accepted:{recipient}:{second_inviter}:2"
    second_job = store.get("ledger_outbox", second_job_id)
    assert second_job and second_job["payload"]["member_id"] == second_inviter
    assert store.get("ledger_outbox", first_job_id) == first_job
    assert store.get("ledger_relationships", f"sponsor:{recipient}")["giver"] == first_inviter
    assert store.get("ledger_relationships", f"sponsor-invite:{second_inviter}:{recipient}")["status"] == "accepted"


def test_recent_reminder_merge_preserves_acceptance_state(joined):
    ledger, store, _, _, _, _ = joined
    member_id = str(oid(1))
    participant = save_receipt(ledger, store, member_id,
        reminder_followup_event="invitation_accepted", reminder_followup_member_id=str(oid(3)),
        reminder_followup_text="Accepted thank you", reminder_followup_restore_needed=True)
    merged = merge_reminder_receipt(ledger, member_id, {
        "reminder_variant_index": 1, "reminder_followup_event": None,
        "reminder_followup_member_id": None, "reminder_followup_text": None,
        "reminder_followup_at": None, "reminder_followup_restore_needed": False},
        expected_generation=participant["consent_generation"], expected_ts=participant["reminder_ts"],
        preserve_followup=True)
    current = ledger.participant(member_id)
    assert merged and current["reminder_variant_index"] == 1
    assert current["reminder_followup_event"] == "invitation_accepted"
    assert current["reminder_followup_text"] == "Accepted thank you"


def test_cli_update_queues_retry_if_acceptance_restoration_fails(joined):
    ledger, store, _, composer, _, slack = joined
    inviter, recipient = str(oid(1)), str(oid(3))
    ledger.sponsor(inviter, recipient)
    invite = store.get("ledger_relationships", f"sponsor-invite:{inviter}:{recipient}")
    invite["at"] = now() - timedelta(days=31)
    store.put("ledger_relationships", invite)
    save_receipt(ledger, store, inviter)
    accepted_text = "Thank you for inviting Maker3 Test. Welcome them in <#CCHAT>."

    def update_with_acceptance_race(**kwargs):
        if slack.chat_update.call_count == 1:
            participant = ledger.participant(inviter)
            participant.update(reminder_followup_event="invitation_accepted",
                reminder_followup_member_id=recipient, reminder_followup_text=accepted_text,
                reminder_followup_restore_needed=False)
            store.put("ledger_participants", participant)
            return {"ts": kwargs["ts"]}
        if slack.chat_update.call_count == 2:
            raise TimeoutError("temporary Slack failure")
        return {"ts": kwargs["ts"]}

    slack.chat_update.side_effect = update_with_acceptance_race
    run(ledger, composer, slack, options(30, member=inviter), stdout=io.StringIO(), stderr=io.StringIO())
    participant = ledger.participant(inviter)
    assert participant["reminder_followup_restore_needed"] is True
    retry = store.get("ledger_outbox", f"sponsorship-reminder-restore:{inviter}:{participant['reminder_ts']}")
    assert retry and retry["kind"] == "sponsorship_reminder_followup"

    worker = Worker(ledger, composer, slack)
    assert worker.step("ledger_outbox", kinds=["sponsorship_reminder_followup"])
    assert slack.chat_update.call_args.kwargs["text"] == accepted_text
    assert ledger.participant(inviter)["reminder_sent_at"] == participant["reminder_sent_at"]


def test_compose_passes_ineligible_ids_through_shared_environment_mapping():
    compose = (Path(__file__).parents[1] / "compose.yaml").read_text(encoding="utf-8")
    assert 'SLACKIDS_INELIGIBLE: "${SLACKIDS_INELIGIBLE:-}"' in compose


def test_delayed_invitation_followup_cannot_overwrite_acceptance(joined):
    ledger, store, _, composer, _, slack = joined
    inviter, accepted = str(oid(1)), str(oid(3))
    save_receipt(ledger, store, inviter)
    ledger.sponsor(inviter, accepted)
    worker = Worker(ledger, composer, slack)
    assert worker.queue_sponsorship_reminder_followup(inviter, "invitation_sent", accepted)
    invited_job = store.get("ledger_outbox", f"sponsorship-reminder-invited:{inviter}:{accepted}")

    ledger.join(accepted, sponsor=inviter)
    accepted_job = store.get("ledger_outbox", f"sponsorship-reminder-accepted:{accepted}:{inviter}:1")
    accepted_job.update(status="working", lease="accepted-lease")
    store.put("ledger_outbox", accepted_job)
    worker.deliver_sponsorship_reminder_followup(accepted_job)
    assert ledger.participant(inviter)["reminder_followup_event"] == "invitation_accepted"

    invited_job.update(status="working", lease="invited-lease")
    store.put("ledger_outbox", invited_job)
    with pytest.raises(Denied, match="superseded"):
        worker.deliver_sponsorship_reminder_followup(invited_job)
    assert slack.chat_update.call_count == 1


def test_failed_accepted_text_restoration_is_retried_by_invitation_worker(joined):
    ledger, store, _, composer, _, slack = joined
    inviter, accepted = str(oid(1)), str(oid(3))
    save_receipt(ledger, store, inviter)
    worker = Worker(ledger, composer, slack)
    assert worker.queue_sponsorship_reminder_followup(inviter, "invitation_sent", accepted)
    invited_job = store.get("ledger_outbox", f"sponsorship-reminder-invited:{inviter}:{accepted}")
    accepted_text = "Thank you for inviting Maker3 Test. Welcome them in <#CCHAT>."
    participant = ledger.participant(inviter)
    participant.update(reminder_followup_event="invitation_accepted", reminder_followup_member_id=accepted,
                       reminder_followup_text=accepted_text, reminder_followup_restore_needed=True)
    store.put("ledger_participants", participant)
    invited_job.update(status="working", lease="invite-first-run")
    store.put("ledger_outbox", invited_job)

    def fail_first_update(**kwargs):
        if slack.chat_update.call_count == 1:
            raise TimeoutError("temporary failure")
        return {"ts": kwargs["ts"]}

    slack.chat_update.side_effect = fail_first_update
    with pytest.raises(TimeoutError):
        worker.deliver_sponsorship_reminder_followup(invited_job)
    assert ledger.participant(inviter)["reminder_followup_restore_needed"] is True

    invited_job.update(status="working", lease="invite-retry")
    store.put("ledger_outbox", invited_job)
    worker.deliver_sponsorship_reminder_followup(invited_job)
    assert slack.chat_update.call_args.kwargs["text"] == accepted_text
    assert ledger.participant(inviter)["reminder_followup_restore_needed"] is False


def test_live_reminder_honors_maintenance_pause_but_dry_run_does_not(joined):
    ledger, store, _, composer, _, slack = joined
    member_id = str(oid(1))
    store.put("ledger_catalog", {"_id": "control", "paused": True})
    stdout, stderr = io.StringIO(), io.StringIO()
    totals = run(ledger, composer, slack, options("never", member=member_id),
        stdout=stdout, stderr=stderr)
    assert totals == {"sent": 0, "updated": 0, "failed": 0, "mongo_updated": 0}
    assert "maintenance" in stderr.getvalue()
    assert stdout.getvalue().count("Sponsorship reminder totals") == 1
    slack.users_info.assert_not_called()
    preview = io.StringIO()
    run(ledger, composer, slack, options("never", member=member_id, dry_run=True),
        stdout=preview, stderr=io.StringIO())
    assert '"member_id"' in preview.getvalue()
    slack.users_info.assert_not_called()


def test_queued_reminder_followup_honors_maintenance_pause(joined):
    ledger, store, _, composer, api, slack = joined
    inviter, accepted = str(oid(1)), str(oid(3))
    save_receipt(ledger, store, inviter)
    ledger.sponsor(inviter, accepted)
    ledger.join(accepted, sponsor=inviter)
    job = store.get("ledger_outbox", f"sponsorship-reminder-accepted:{accepted}:{inviter}:1")
    job.update(status="working", lease="paused-followup")
    store.put("ledger_outbox", job)
    store.put("ledger_catalog", {"_id": "control", "paused": True})
    with pytest.raises(ReviewDeliveryBusy):
        Worker(ledger, composer, slack).outbox(job)
    api.complete.assert_not_called()
    slack.chat_update.assert_not_called()
    slack.chat_postMessage.assert_not_called()


def test_invitation_delivery_followup_is_only_queued_for_recent_receipt(joined):
    ledger, store, _, composer, _, slack = joined
    worker = Worker(ledger, composer, slack)
    inviter, recipient = str(oid(1)), str(oid(3))
    assert not worker.queue_sponsorship_reminder_followup(inviter, "invitation_sent", recipient)
    save_receipt(ledger, store, inviter)
    assert worker.queue_sponsorship_reminder_followup(inviter, "invitation_sent", recipient)
    assert store.get("ledger_outbox", f"sponsorship-reminder-invited:{inviter}:{recipient}")


def test_successful_sponsor_invitation_delivery_queues_followup(joined):
    ledger, store, _, composer, _, slack = joined
    inviter, recipient = str(oid(1)), str(oid(3))
    save_receipt(ledger, store, inviter)
    ledger.sponsor(inviter, recipient)
    invitation_job = store.select("ledger_outbox", {"kind": "message", "payload.type": "invitation"})[0]
    invitation_job.update(status="working", lease="test-lease")
    store.put("ledger_outbox", invitation_job)

    worker = Worker(ledger, composer, slack)
    worker.outbox(invitation_job)

    followup = store.get("ledger_outbox", f"sponsorship-reminder-invited:{inviter}:{recipient}")
    assert followup and followup["payload"]["event"] == "invitation_sent"


def test_verbose_reports_slack_429_and_prints_failure_summary(joined):
    ledger, _, _, composer, _, slack = joined

    class Response(dict):
        status_code = 429
        headers = {"Retry-After": "19"}

    slack.chat_postMessage.side_effect = SlackApiError("ratelimited", Response(error="ratelimited"))
    stdout, stderr = io.StringIO(), io.StringIO()
    totals = run(ledger, composer, slack, options("never", verbose=True), stdout=stdout, stderr=stderr)
    assert totals["failed"] == 2
    assert "Slack 429 backoff" in stderr.getvalue()
    assert '"failed_message_operations": 2' in stdout.getvalue()


def test_debug_reports_mongo_receipt_write_error_without_counting_send_failure(joined):
    ledger, store, _, composer, _, slack = joined
    original_put = store.put

    def put(collection, document):
        if (collection == "ledger_participants" and document.get("member_id") == str(oid(2))
                and document.get("reminder_ts")):
            raise PyMongoError("database unavailable")
        return original_put(collection, document)

    with patch.object(store, "put", side_effect=put):
        stdout, stderr = io.StringIO(), io.StringIO()
        totals = run(ledger, composer, slack, options("never", debug=True), stdout=stdout, stderr=stderr)
    assert totals["sent"] == 2
    assert totals["failed"] == 0
    assert totals["mongo_updated"] == 1
    assert "Mongo operation failed" in stderr.getvalue()
    assert '"messages_sent": 2' in stdout.getvalue()
