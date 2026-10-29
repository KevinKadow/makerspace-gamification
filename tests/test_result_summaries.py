"""Action result receipts remain bounded, durable, and independently authorized."""
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from conftest import oid
from ledger.domain import Denied
from ledger import result_summaries as summaries
from ledger.storage import now
from ledger.worker import Worker


def worker(env):
    ledger, _, _, composer, _, slack = env
    slack.chat_update.return_value = {"ts": "123.456"}
    result = Worker(ledger, composer, slack, bot_id="UBOT")
    result.persist_composition = MagicMock(return_value={"text": "The ink has dried.", "outcome": "generated", "generation_ms": 2})
    return result


def claim(store, key, lease="test"):
    job = store.get("ledger_outbox", key)
    job.update(status="working", lease=lease, attempts=1)
    store.put("ledger_outbox", job)
    return job


def flush(worker, owner_id):
    owner = worker.store.get("ledger_evidence", owner_id)
    key = f"summary-flush:{owner_id}:{owner['content_revision']}"
    summaries.flush(worker, claim(worker.store, key))
    current = worker.store.get("ledger_evidence", owner_id)
    return claim(worker.store, current["snapshot_job_id"])


def receipt(ledger, evidence_id, destination, status):
    def write(store):
        evidence = store.get("ledger_evidence", evidence_id)
        evidence["deliveries"][destination] = {"status": status, "at": now()}
        store.put("ledger_evidence", evidence)
        return summaries.track_kudos(ledger, evidence)
    return ledger.store.atomic(write)


def kudos(env, key="summary", public=True):
    ledger, *_ = env
    evidence = ledger.kudos(str(oid(1)), str(oid(2)), "Original authored text stays private.",
        key=key, public=public, expected_participation=True)
    return evidence, summaries.track_kudos(ledger, evidence)


def expire(store, owner_id):
    owner = store.get("ledger_evidence", owner_id)
    owner["deadline"] = now() - timedelta(seconds=1)
    store.put("ledger_evidence", owner)


@pytest.mark.parametrize("order", [("recipient", "shared"), ("shared", "recipient")])
def test_completed_kudos_posts_one_final_receipt_in_either_delivery_order(joined, order):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    evidence, owner_id = kudos(joined)
    deadline = store.get("ledger_evidence", owner_id)["deadline"]
    receipt(ledger, evidence["_id"], order[0], "delivered")
    pending = store.get("ledger_evidence", owner_id)
    with pytest.raises(summaries.SummaryPending):
        summaries.flush(w, claim(store, f"summary-flush:{owner_id}:{pending['content_revision']}"))
    receipt(ledger, evidence["_id"], order[1], "delivered")
    job = flush(w, owner_id)
    summaries.deliver(w, job)
    call = slack.chat_postMessage.call_args.kwargs
    assert "Your kudos reached <@U2> in DM and Ledge Chat. 17 XP was awarded." in call["text"]
    assert "Original authored text" not in str(w.persist_composition.call_args)
    assert "xp_total" not in job["payload"]["facts_snapshot"]
    assert call["channel"] == "DU1"
    assert store.get("ledger_evidence", owner_id)["deadline"] == deadline
    assert slack.chat_postMessage.call_count == 1
    assert w.persist_composition.call_args.kwargs["profile"] == "receipt"
    summaries.deliver(w, job)
    assert slack.chat_postMessage.call_count == 1


def test_private_kudos_does_not_invent_public_destination(joined):
    ledger, _, _, _, _, slack = joined
    w = worker(joined)
    evidence, owner_id = kudos(joined, public=False)
    receipt(ledger, evidence["_id"], "recipient", "delivered")
    summaries.deliver(w, flush(w, owner_id))
    text = slack.chat_postMessage.call_args.kwargs["text"]
    assert "in DM." in text and "Ledge Chat" not in text


def test_pending_at_deadline_is_plain_then_late_delivery_edits_parent(joined):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    evidence, owner_id = kudos(joined)
    expire(store, owner_id)
    first = flush(w, owner_id)
    summaries.deliver(w, first)
    assert "still pending" in slack.chat_postMessage.call_args.kwargs["text"]
    assert "17 XP was awarded" in slack.chat_postMessage.call_args.kwargs["text"]
    w.persist_composition.assert_not_called()
    receipt(ledger, evidence["_id"], "recipient", "delivered")
    summaries.deliver(w, flush(w, owner_id))
    assert "in DM." in slack.chat_update.call_args.kwargs["text"]
    assert "Ledge Chat: pending." in slack.chat_update.call_args.kwargs["text"]
    w.persist_composition.assert_not_called()
    receipt(ledger, evidence["_id"], "shared", "delivered")
    summaries.deliver(w, flush(w, owner_id))
    assert slack.chat_postMessage.call_count == 1
    assert slack.chat_update.call_count == 2
    assert slack.chat_update.call_args.kwargs["ts"] == "123.456"
    owner = store.get("ledger_evidence", owner_id)
    assert owner["post_count"] == 1 and owner["update_count"] == 2
    assert ledger.participant(str(oid(2)))["xp"] == "17"


def test_failed_destination_stays_plain_and_operator_success_edits_parent(joined):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    evidence, owner_id = kudos(joined)
    receipt(ledger, evidence["_id"], "recipient", "delivered")
    receipt(ledger, evidence["_id"], "shared", "failed")
    summaries.deliver(w, flush(w, owner_id))
    assert "Ledge Chat: failed." in slack.chat_postMessage.call_args.kwargs["text"]
    w.persist_composition.assert_not_called()
    receipt(ledger, evidence["_id"], "shared", "delivered")
    summaries.deliver(w, flush(w, owner_id))
    assert "in DM and Ledge Chat." in slack.chat_update.call_args.kwargs["text"]
    assert ledger.participant(str(oid(2)))["xp"] == "17"


def test_game_results_deduplicate_awards_and_keep_unrelated_actions_separate(joined):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    member = str(oid(1))
    owner_id = summaries.collect(ledger, "a", member, "challenge", {"award_id": "award1", "xp_change": "1.25", "challenge": "Small build"}, "award1")
    summaries.collect(ledger, "a", member, "challenge", {"award_id": "award1", "xp_change": "1.25", "challenge": "Small build"}, "award1")
    summaries.collect(ledger, "a", member, "rank_up", {"award_id": "award1", "xp_change": "1.25", "rank": "Novice"}, "rank1")
    summaries.collect(ledger, "a", member, "shop_complete", {"shop": "Wood"}, "shop1")
    other = summaries.collect(ledger, "b", member, "quest", {"summary": "Quest authoring unlocked."}, "unlock")
    summaries.finish_action(ledger, "a")
    job = flush(w, owner_id)
    summaries.deliver(w, job)
    text = slack.chat_postMessage.call_args.kwargs["text"]
    assert "You earned 1.25 XP." in text
    assert "Rank reached: Novice." in text and "Shop completed: Wood." in text
    assert "authoring" not in text
    assert len(store.get("ledger_evidence", owner_id)["events"]) == 3
    assert not store.get("ledger_evidence", other)["complete"]
    button = slack.chat_postMessage.call_args.kwargs["blocks"][-1]["elements"][0]
    assert button["action_id"] == "guidance_next_step" and button["value"] == owner_id


def test_paused_worker_cancels_game_summary_flush_and_queued_delivery(joined):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    member = str(oid(1))

    flush_owner = summaries.collect(ledger, "paused-flush", member, "challenge",
                                    {"xp_change": "5"}, "award1")
    summaries.finish_action(ledger, "paused-flush")
    store.put("ledger_catalog", {"_id": "control", "paused": True})
    flush_revision = store.get("ledger_evidence", flush_owner)["content_revision"]
    with pytest.raises(Denied, match="paused"):
        w.outbox(claim(store, f"summary-flush:{flush_owner}:{flush_revision}"))
    assert store.get("ledger_evidence", flush_owner).get("snapshot_job_id") is None

    store.put("ledger_catalog", {"_id": "control", "paused": False})
    delivery_owner = summaries.collect(ledger, "paused-delivery", member, "rank_up",
                                       {"rank": "Novice"}, "rank1")
    summaries.finish_action(ledger, "paused-delivery")
    delivery = flush(w, delivery_owner)
    store.put("ledger_catalog", {"_id": "control", "paused": True})
    with pytest.raises(Denied, match="paused"):
        w.outbox(delivery)
    slack.chat_postMessage.assert_not_called()
    w.persist_composition.assert_not_called()


def test_paused_worker_still_delivers_peer_kudos_summary_exception(joined):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    evidence, owner_id = kudos(joined, public=False)
    receipt(ledger, evidence["_id"], "recipient", "delivered")
    store.put("ledger_catalog", {"_id": "control", "paused": True})
    revision = store.get("ledger_evidence", owner_id)["content_revision"]
    flush_job = claim(store, f"summary-flush:{owner_id}:{revision}")
    w.outbox(flush_job)
    owner = store.get("ledger_evidence", owner_id)
    w.outbox(claim(store, owner["snapshot_job_id"]))
    assert slack.chat_postMessage.call_args.kwargs["channel"] == "DU1"


def test_event_during_generation_supersedes_old_revision(joined):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    member = str(oid(1))
    owner_id = summaries.collect(ledger, "racing", member, "challenge", {"xp_change": "5"}, "award1")
    summaries.finish_action(ledger, "racing")
    old = flush(w, owner_id)
    def compose(*args, **kwargs):
        summaries.collect(ledger, "racing", member, "rank_up", {"rank": "Novice"}, "rank1")
        flush(w, owner_id)
        return {"text": "Recorded.", "outcome": "generated"}
    w.persist_composition.side_effect = compose
    summaries.deliver(w, old)
    slack.chat_postMessage.assert_not_called()
    w.persist_composition.side_effect = None
    latest = store.get("ledger_evidence", owner_id)
    summaries.deliver(w, claim(store, latest["snapshot_job_id"]))
    assert "Rank reached: Novice." in slack.chat_postMessage.call_args.kwargs["text"]
    assert store.get("ledger_outbox", old["_id"])["payload"]["facts_snapshot"] == old["payload"]["facts_snapshot"]


def test_event_during_slack_post_survives_receipt_merge_and_updates(joined):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    member = str(oid(1))
    owner_id = summaries.collect(ledger, "posting", member, "challenge", {"xp_change": "5"}, "award1")
    summaries.finish_action(ledger, "posting")
    first = flush(w, owner_id)
    def post(**kwargs):
        summaries.collect(ledger, "posting", member, "shop_complete", {"shop": "Wood"}, "shop1")
        flush(w, owner_id)
        return {"ts": "123.456"}
    slack.chat_postMessage.side_effect = post
    summaries.deliver(w, first)
    owner = store.get("ledger_evidence", owner_id)
    assert len(owner["events"]) == 2 and owner["delivered_revision"] < owner["snapshot_revision"]
    slack.chat_postMessage.side_effect = None
    summaries.deliver(w, claim(store, owner["snapshot_job_id"]))
    assert "Shop completed: Wood." in slack.chat_update.call_args.kwargs["text"]
    assert slack.chat_postMessage.call_count == 1


def test_timeout_retry_keeps_saved_text_and_stable_post_identifier(joined):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    evidence, owner_id = kudos(joined, public=False)
    receipt(ledger, evidence["_id"], "recipient", "delivered")
    job = flush(w, owner_id)
    slack.chat_postMessage.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        summaries.deliver(w, job)
    first = slack.chat_postMessage.call_args.kwargs
    saved_text = store.get("ledger_outbox", job["_id"])["rendered_text"]
    w.persist_composition.return_value = {"text": "A different voice.", "outcome": "generated"}
    slack.chat_postMessage.side_effect = None
    summaries.deliver(w, claim(store, job["_id"], "retry"))
    second = slack.chat_postMessage.call_args.kwargs
    assert first["client_msg_id"] == second["client_msg_id"]
    assert second["text"] == saved_text
    assert w.persist_composition.call_count == 1


def test_concurrent_owner_lock_defers_without_sending(joined):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    owner_id = summaries.collect(ledger, "locked", str(oid(1)), "challenge", {"xp_change": "5"}, "award1")
    summaries.finish_action(ledger, "locked")
    job = flush(w, owner_id)
    owner = store.get("ledger_evidence", owner_id)
    owner.update(delivery_lock="another", delivery_lock_until=now() + timedelta(seconds=60))
    store.put("ledger_evidence", owner)
    with pytest.raises(summaries.SummaryBusy):
        summaries.deliver(w, job)
    slack.chat_postMessage.assert_not_called()


@pytest.mark.parametrize("rejoin", [False, True])
def test_game_consent_rechecked_after_generation_and_rejoin_cannot_revive(joined, rejoin):
    ledger, _, _, _, _, slack = joined
    w = worker(joined)
    member = str(oid(1))
    owner_id = summaries.collect(ledger, "consent", member, "challenge", {"xp_change": "5"}, "award1")
    summaries.finish_action(ledger, "consent")
    job = flush(w, owner_id)
    def compose(*args, **kwargs):
        ledger.leave(member)
        if rejoin:
            ledger.join(member)
        return {"text": "Recorded.", "outcome": "generated"}
    w.persist_composition.side_effect = compose
    with pytest.raises(Denied):
        summaries.deliver(w, job)
    slack.chat_postMessage.assert_not_called()


def test_peer_receipt_is_allowed_after_giver_opts_out(joined):
    ledger, _, _, _, _, slack = joined
    w = worker(joined)
    evidence, owner_id = kudos(joined, public=False)
    receipt(ledger, evidence["_id"], "recipient", "delivered")
    ledger.leave(str(oid(1)))
    summaries.deliver(w, flush(w, owner_id))
    assert slack.chat_postMessage.call_args.kwargs["channel"] == "DU1"


@pytest.mark.parametrize("error", ["message_not_found", "ratelimited"])
def test_only_deleted_parent_is_replaced(joined, error):
    ledger, _, _, _, _, slack = joined
    w = worker(joined)
    evidence, owner_id = kudos(joined)
    expire(w.store, owner_id)
    summaries.deliver(w, flush(w, owner_id))
    receipt(ledger, evidence["_id"], "recipient", "delivered")
    job = flush(w, owner_id)
    slack.chat_update.side_effect = SlackApiError("Delivery failed", {"error": error})
    if error == "message_not_found":
        summaries.deliver(w, job)
        assert slack.chat_postMessage.call_count == 2
    else:
        with pytest.raises(SlackApiError):
            summaries.deliver(w, job)
        assert slack.chat_postMessage.call_count == 1


def test_failed_narration_omits_flourish_but_keeps_facts(joined):
    ledger, _, _, _, _, slack = joined
    w = worker(joined)
    evidence, owner_id = kudos(joined, public=False)
    receipt(ledger, evidence["_id"], "recipient", "delivered")
    w.persist_composition.return_value = {"text": "Generic canned fallback", "outcome": "fallback", "fallback_reason": "TimeoutError"}
    job = flush(w, owner_id)
    summaries.deliver(w, job)
    assert slack.chat_postMessage.call_args.kwargs["text"] == "Your kudos reached <@U2> in DM. 17 XP was awarded."
    metrics = w.store.get("ledger_outbox", job["_id"])["delivery_metrics"]
    assert metrics["fallback_reason"] == "TimeoutError"
    assert "summary_latency_ms" in metrics and "text" not in metrics


def test_repeat_receipts_and_flushes_create_no_new_delivery_revision(joined):
    ledger, store, *_ = joined
    w = worker(joined)
    evidence, owner_id = kudos(joined, public=False)
    receipt(ledger, evidence["_id"], "recipient", "delivered")
    job = flush(w, owner_id)
    before = store.get("ledger_evidence", owner_id)
    receipt(ledger, evidence["_id"], "recipient", "delivered")
    again = flush(w, owner_id)
    assert store.get("ledger_evidence", owner_id)["content_revision"] == before["content_revision"]
    assert job["_id"] == again["_id"]


def test_new_worker_recovers_posted_parent_from_durable_owner(joined):
    ledger, _, _, _, _, slack = joined
    first = worker(joined)
    evidence, owner_id = kudos(joined)
    expire(first.store, owner_id)
    summaries.deliver(first, flush(first, owner_id))
    second = worker(joined)
    receipt(ledger, evidence["_id"], "recipient", "delivered")
    receipt(ledger, evidence["_id"], "shared", "delivered")
    summaries.deliver(second, flush(second, owner_id))
    assert slack.chat_postMessage.call_count == 1 and slack.chat_update.call_count == 1


def test_new_revision_after_uncertain_post_recovers_parent_then_updates_latest_text(joined):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    evidence, owner_id = kudos(joined)
    expire(store, owner_id)
    first = flush(w, owner_id)
    slack.chat_postMessage.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        summaries.deliver(w, first)
    token = slack.chat_postMessage.call_args.kwargs["client_msg_id"]
    receipt(ledger, evidence["_id"], "recipient", "delivered")
    receipt(ledger, evidence["_id"], "shared", "delivered")
    latest = flush(w, owner_id)
    # Model Slack accepting an original post whose API response was lost, then
    # returning that same timestamp for its stable-token retry.
    slack.chat_postMessage.side_effect = None
    slack.chat_postMessage.return_value = {"ts": "original.uncertain"}
    slack.chat_update.return_value = {"ts": "original.uncertain"}
    summaries.deliver(w, latest)
    assert slack.chat_postMessage.call_args.kwargs["client_msg_id"] == token
    assert "in DM and Ledge Chat." in slack.chat_update.call_args.kwargs["text"]
    assert slack.chat_update.call_args.kwargs["ts"] == "original.uncertain"
    assert store.get("ledger_evidence", owner_id)["ts"] == "original.uncertain"
    assert store.get("ledger_outbox", latest["_id"])["delivery_metrics"]["operation"] == "post_and_update"


def test_large_reconciliation_keeps_all_xp_and_bounds_message_detail(joined):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    member = str(oid(1))
    for index in range(35):
        owner_id = summaries.collect(ledger, "large", member, "clearance", {
            "xp_change": "1.25", "award_id": f"award{index}", "tool_name": f"Tool{index} " + "&" * 500}, f"award{index}")
    summaries.finish_action(ledger, "large")
    job = flush(w, owner_id)
    summaries.deliver(w, job)
    call = slack.chat_postMessage.call_args.kwargs
    assert "You earned 43.75 XP." in call["text"]
    assert "25 additional updates" in call["text"]
    assert len(job["payload"]["facts_snapshot"]["achievements"]) == 10
    assert all(len(block["text"]["text"]) < 3000 for block in call["blocks"] if block["type"] == "section")
    assert len(store.get("ledger_evidence", owner_id)["events"]) == 35


def test_completion_without_positive_award_reports_title_and_no_xp(joined):
    ledger, _, _, _, _, slack = joined
    w = worker(joined)
    owner_id = summaries.collect(ledger, "verified", str(oid(1)), "quest", {
        "quest_title": "Make a safe small build", "summary": "Independently verified milestone.",
        "verified_milestone": True, "xp_outcome_known": True}, "verified1")
    summaries.finish_action(ledger, "verified")
    summaries.deliver(w, flush(w, owner_id))
    text = slack.chat_postMessage.call_args.kwargs["text"]
    assert "No XP was awarded." in text and "Quest completed: Make a safe small build." in text
    assert "Independently verified milestone." not in text


def test_event_during_dm_open_supersedes_old_post(joined):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    member = str(oid(1))
    owner_id = summaries.collect(ledger, "opening", member, "challenge", {"xp_change": "5"}, "award1")
    summaries.finish_action(ledger, "opening")
    first = flush(w, owner_id)
    def open_dm(**kwargs):
        summaries.collect(ledger, "opening", member, "rank_up", {"rank": "Novice"}, "rank1")
        flush(w, owner_id)
        return {"channel": {"id": "DU1"}}
    slack.conversations_open.side_effect = open_dm
    summaries.deliver(w, first)
    slack.chat_postMessage.assert_not_called()
    slack.conversations_open.side_effect = lambda **kwargs: {"channel": {"id": "DU1"}}
    summaries.deliver(w, claim(store, store.get("ledger_evidence", owner_id)["snapshot_job_id"]))
    assert "Rank reached: Novice." in slack.chat_postMessage.call_args.kwargs["text"]


def test_opt_out_during_final_identity_lookup_cancels_game_delivery(joined):
    ledger, _, _, _, _, slack = joined
    w = worker(joined)
    member = str(oid(1))
    owner_id = summaries.collect(ledger, "lookup", member, "challenge", {"xp_change": "5"}, "award1")
    summaries.finish_action(ledger, "lookup")
    job = flush(w, owner_id)
    count = 0
    def users_info(**kwargs):
        nonlocal count
        count += 1
        if count == 3:
            ledger.leave(member)
        return {"user": {"id": "U1", "deleted": False, "is_bot": False}}
    slack.users_info.side_effect = users_info
    with pytest.raises(Denied):
        summaries.deliver(w, job)
    slack.chat_postMessage.assert_not_called()


def test_expired_owner_lock_is_recoverable(joined):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    member = str(oid(1))
    owner_id = summaries.collect(ledger, "expired", member, "challenge", {"xp_change": "5"}, "award1")
    summaries.finish_action(ledger, "expired")
    job = flush(w, owner_id)
    owner = store.get("ledger_evidence", owner_id)
    owner.update(delivery_lock="dead-process", delivery_lock_until=now() - timedelta(seconds=1))
    store.put("ledger_evidence", owner)
    summaries.deliver(w, job)
    slack.chat_postMessage.assert_called_once()


@pytest.mark.parametrize("delivered,other", [("recipient", "failed"), ("recipient", "cancelled"),
                                            ("shared", "failed"), ("shared", "cancelled")])
def test_terminal_partial_result_remains_partial_and_plain(joined, delivered, other):
    ledger, _, _, _, _, slack = joined
    w = worker(joined)
    evidence, owner_id = kudos(joined)
    receipt(ledger, evidence["_id"], delivered, "delivered")
    receipt(ledger, evidence["_id"], "shared" if delivered == "recipient" else "recipient", other)
    job = flush(w, owner_id)
    facts = job["payload"]["facts_snapshot"]
    assert facts["delivery_status"] == "partial" and facts["complete"] and not facts["successful"]
    summaries.deliver(w, job)
    text = slack.chat_postMessage.call_args.kwargs["text"]
    assert "in DM." in text if delivered == "recipient" else "in Ledge Chat." in text
    assert ("Ledge Chat" if delivered == "recipient" else "DM") + ": " + other in text
    w.persist_composition.assert_not_called()


@pytest.mark.parametrize("finished,known", [(True, False), (False, True), (False, False)])
def test_missing_or_unfinished_xp_outcome_does_not_claim_no_xp(joined, finished, known):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    owner_id = summaries.collect(ledger, "unknown-xp", str(oid(1)), "quest", {
        "quest_title": "A verified completion", "verified_milestone": True, "xp_outcome_known": known}, "verified1")
    if finished:
        summaries.finish_action(ledger, "unknown-xp")
    else:
        expire(store, owner_id)
    job = flush(w, owner_id)
    assert not job["payload"]["facts_snapshot"]["xp_outcome_known"]
    summaries.deliver(w, job)
    assert "No XP was awarded" not in slack.chat_postMessage.call_args.kwargs["text"]


@pytest.mark.parametrize("kind,title_field", [("boss", "quest_title"), ("stewardship", "challenge_title")])
def test_named_milestone_replaces_generic_duplicate_without_losing_xp(joined, kind, title_field):
    ledger, _, _, _, _, slack = joined
    w = worker(joined)
    member = str(oid(1))
    owner_id = summaries.collect(ledger, "named", member, kind, {"xp_change": "17", "award_id": "award1"}, "award1")
    summaries.collect(ledger, "named", member, kind, {title_field: "Build a community tool",
        "xp_change": "17", "award_id": "award1", "verified_milestone": True, "xp_outcome_known": True}, "verified1")
    summaries.finish_action(ledger, "named")
    summaries.deliver(w, flush(w, owner_id))
    text = slack.chat_postMessage.call_args.kwargs["text"]
    assert "You earned 17 XP." in text
    assert "milestone completed: Build a community tool." in text
    assert text.count("milestone completed") == 1


def test_one_unknown_verified_outcome_prevents_zero_xp_claim_for_whole_action(joined):
    ledger, _, _, _, _, slack = joined
    w = worker(joined)
    member = str(oid(1))
    owner_id = summaries.collect(ledger, "mixed-outcome", member, "quest", {
        "quest_title": "Known zero", "verified_milestone": True, "xp_outcome_known": True}, "known")
    summaries.collect(ledger, "mixed-outcome", member, "quest", {
        "quest_title": "Unknown award", "verified_milestone": True}, "unknown")
    summaries.finish_action(ledger, "mixed-outcome")
    summaries.deliver(w, flush(w, owner_id))
    assert "No XP was awarded" not in slack.chat_postMessage.call_args.kwargs["text"]
