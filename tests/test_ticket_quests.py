from datetime import timedelta
from hashlib import sha1
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest
import requests
from PIL import Image
from slack_sdk.errors import SlackApiError

from conftest import oid
from ledger.review_notifications import ReviewDeliveryBusy
from ledger.storage import now
from ledger.ticket_quests import TicketQuests, canonical_id, config, sanitize_jpeg, ticket_key, validate_config
from ledger.worker import Worker


def setup_tickets(joined, *, reporter=1, status="open", category="broken"):
    ledger, store, source, *_ = joined
    ledger.join(str(oid(1)))
    ledger.join(str(oid(2)))
    source.data["fix_tickets"].append({"_id": 735, "reporter_id": oid(reporter), "category": category,
        "status": status, "created_at": now(), "updated_at": now(), "revision": 1})
    service = TicketQuests(ledger)
    service.save_config(str(oid(10)), {"quest_channel_id": "CQUEST", "fix_channel_id": "CFIX",
        "duration_hours": 18, "xp_with_image": 100, "xp_without_image": 66})
    source.data["fix_tickets"][-1]["created_at"] = now()
    return ledger, store, source, service


def open_quest(service, store, source, ticket_id=735):
    service.reconcile(ticket_id)
    quest = store.get("ledger_evidence", ticket_key(ticket_id))
    quest.update(status="open", announcement_ts="900.001", announcement_channel="CQUEST",
                 started_at=now(), deadline=now() + timedelta(hours=18))
    store.put("ledger_evidence", quest)
    return quest


def test_ticket_id_supports_sequence_and_legacy_objectid():
    assert canonical_id(735) == "735"
    assert canonical_id(str(oid(800))) == str(oid(800))
    with pytest.raises(ValueError):
        canonical_id(0)


def test_config_is_validated_and_quests_are_off_until_enabled(joined):
    ledger, store, source, *_ = joined
    service = TicketQuests(ledger)
    source.data["fix_tickets"].append({"_id": 735, "reporter_id": oid(2), "category": "broken", "status": "open"})
    service.reconcile(735)
    assert store.get("ledger_evidence", ticket_key(735)) is None
    with pytest.raises(ValueError, match="channel ID"):
        validate_config({"quest_channel_id": "bad", "fix_channel_id": "CFIX", "duration_hours": 18,
                         "xp_with_image": 100, "xp_without_image": 66})


def test_ticket_event_hint_creates_once_and_uses_current_ticket_state(joined):
    ledger, store, source, service = setup_tickets(joined)
    service.reconcile(735)
    service.reconcile(735)
    quest = store.get("ledger_evidence", ticket_key(735))
    assert quest["status"] == "announcing"
    assert len(store.select("ledger_outbox", {"kind": "ticket_quest_announce"})) == 1
    source.data["fix_tickets"][0]["status"] = "resolved"
    service.reconcile(735)
    # Terminal state before announcement delivery cancels the stale announcement.
    assert store.get("ledger_evidence", ticket_key(735))["status"] == "closed"
    assert store.get("ledger_outbox", "ticket-quest-announcement:735")["status"] == "cancelled"


def test_first_sentence_claim_awards_exactly_once_and_excludes_reporter(joined):
    ledger, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    service.response_event({"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "901.001", "text": "I checked the switch and confirmed the motor is jammed."}, "event-1")
    assert store.get("ledger_evidence", quest["_id"])["winner_member_id"] == str(oid(2))
    assert ledger.participant(str(oid(2)))["xp"] == "66"
    service.response_event({"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "901.001", "text": "I checked the switch and confirmed the motor is jammed."}, "event-1-retry")
    assert ledger.participant(str(oid(2)))["xp"] == "66"
    # Reporter is excluded even when opted in; their reply is recorded but never wins.
    service.response_event({"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U1", "ts": "901.002", "text": "I looked at this ticket."}, "event-2")
    assert store.get("ledger_evidence", quest["_id"])["winner_member_id"] == str(oid(2))


def test_jpeg_claim_awards_configured_100_and_two_concurrent_claims_have_one_winner(joined):
    ledger, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    service.worker = MagicMock()
    with patch.object(TicketQuests, "_download_jpeg", return_value=b"jpeg"):
        service.response_event({"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
            "user": "U2", "ts": "903.001", "text": "I checked the belt and confirmed the issue.",
            "files": [{"id": "F1", "mimetype": "image/jpeg"}]}, "event-image")
    assert ledger.participant(str(oid(2)))["xp"] == "100"

    source.data["fix_tickets"].append({"_id": 736, "reporter_id": oid(1), "category": "damaged", "status": "open", "created_at": now()})
    service.worker = None
    service.reconcile(736)
    race = store.get("ledger_evidence", ticket_key(736))
    race.update(status="open", announcement_ts="904.001", announcement_channel="CQUEST", deadline=now() + timedelta(hours=18))
    store.put("ledger_evidence", race)
    ledger.join(str(oid(3)))
    claims = [("U2", "904.002"), ("U3", "904.003")]
    def claim(item):
        uid, ts = item
        return service.response_event({"type": "message", "channel": "CQUEST", "thread_ts": race["announcement_ts"],
            "user": uid, "ts": ts, "text": "I inspected the tool and confirmed the damaged part."}, ts)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(claim, claims))
    race = store.get("ledger_evidence", race["_id"])
    assert race["winner_member_id"] in {str(oid(2)), str(oid(3))}
    assert sum(int(ledger.participant(str(oid(i)))["xp"]) for i in (2, 3)) == 166


def test_transient_image_check_retries_before_awarding_image_xp(joined):
    ledger, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    service.worker = MagicMock()
    event = {"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "903.101", "text": "I checked the belt and confirmed the issue.",
        "files": [{"id": "F1", "mimetype": "image/jpeg"}]}
    with patch.object(TicketQuests, "_download_jpeg", side_effect=requests.ConnectionError("temporary network error")):
        with pytest.raises(requests.RequestException):
            service.response_event(event, "transient-image-event")
    assert ledger.participant(str(oid(2)))["xp"] == "0"
    pending = store.get("ledger_evidence", quest["_id"]).get("pending_claim")
    assert pending["response_id"] == "ticket-quest-response:" + sha1(
        f"CQUEST:{event['ts']}".encode()).hexdigest()
    assert not store.select("ledger_evidence", {"kind": "broken_ticket_quest_response"})

    ledger.join(str(oid(3)))
    later = {"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U3", "ts": "903.102", "text": "I checked the tool and confirmed the issue."}
    with pytest.raises(ReviewDeliveryBusy):
        service.response_event(later, "later-text-reply")
    assert ledger.participant(str(oid(3)))["xp"] == "0"
    with patch.object(TicketQuests, "_download_jpeg", return_value=b"sanitized-jpeg"):
        assert service.response_event(event, "transient-image-event") is True
    assert ledger.participant(str(oid(2)))["xp"] == "100"
    assert service.response_event(later, "later-text-reply") is True
    assert ledger.participant(str(oid(3)))["xp"] == "0"


@pytest.mark.parametrize(("change", "outcome", "reason"), [
    ("resolved", "closed", "ticket_closed"), ("rejected", "closed", "ticket_closed"),
    ("withdrawn", "closed", "ticket_closed"), ("deleted", "deleted", "ticket_deleted"),
    ("recategorized", "ineligible", "ticket_ineligible")])
def test_pending_jpeg_claim_closes_when_ticket_becomes_ineligible(joined, change, outcome, reason):
    ledger, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    service.worker = MagicMock()
    event = {"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "903.111", "text": "I checked the belt and confirmed the issue.",
        "files": [{"id": "F1", "mimetype": "image/jpeg"}]}
    with patch.object(TicketQuests, "_download_jpeg", side_effect=requests.ConnectionError("temporary")):
        with pytest.raises(requests.RequestException):
            service.response_event(event, "pending-terminal-ticket")
    if change == "deleted":
        source.data["fix_tickets"].clear()
    elif change == "recategorized":
        source.data["fix_tickets"][0]["category"] = "other"
    else:
        source.data["fix_tickets"][0]["status"] = change

    with patch.object(TicketQuests, "_download_jpeg") as download:
        assert service.response_event(event, "pending-terminal-ticket") is True

    download.assert_not_called()
    saved = store.get("ledger_evidence", quest["_id"])
    assert saved["status"] == "closed" and saved["outcome"] == outcome
    assert not saved.get("pending_claim")
    response = store.get("ledger_evidence", "ticket-quest-response:" + sha1(
        f"CQUEST:{event['ts']}".encode()).hexdigest())
    assert response["rejection_reason"] == reason
    assert ledger.participant(str(oid(2)))["xp"] == "0"


def test_ticket_becoming_ineligible_during_image_verification_cannot_award(joined):
    ledger, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    service.worker = MagicMock()
    event = {"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "903.112", "text": "I checked the belt and confirmed the issue.",
        "files": [{"id": "F1", "mimetype": "image/jpeg"}]}

    def verify_then_resolve(_):
        source.data["fix_tickets"][0]["status"] = "resolved"
        return b"sanitized-jpeg"

    with patch.object(TicketQuests, "_download_jpeg", side_effect=verify_then_resolve):
        assert service.response_event(event, "ticket-changed-during-image-check") is True

    saved = store.get("ledger_evidence", quest["_id"])
    assert saved["status"] == "closed" and saved["outcome"] == "closed"
    assert not saved.get("pending_claim")
    assert ledger.participant(str(oid(2)))["xp"] == "0"


def test_reserved_image_claim_is_released_after_opt_out_and_rejoin(joined):
    ledger, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    service.worker = MagicMock()
    event = {"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "903.111", "text": "I checked the belt and confirmed the issue.",
        "files": [{"id": "F1", "mimetype": "image/jpeg"}]}
    with patch.object(TicketQuests, "_download_jpeg", side_effect=requests.ConnectionError("temporary")):
        with pytest.raises(requests.RequestException):
            service.response_event(event, "generation-change")
    old_generation = ledger.participant(str(oid(2)))["consent_generation"]
    assert store.get("ledger_evidence", quest["_id"])["pending_claim"]["consent_generation"] == old_generation
    ledger.leave(str(oid(2)))
    ledger.join(str(oid(2)))
    assert ledger.participant(str(oid(2)))["consent_generation"] != old_generation

    assert service.response_event(event, "generation-change") is True
    saved = store.get("ledger_evidence", quest["_id"])
    response_id = "ticket-quest-response:" + sha1(f"CQUEST:{event['ts']}".encode()).hexdigest()
    assert "pending_claim" not in saved
    assert store.get("ledger_evidence", response_id)["rejection_reason"] == "consent_generation_changed"
    assert ledger.participant(str(oid(2)))["xp"] == "0"


def test_reserved_image_claim_is_released_when_slack_mapping_disappears(joined):
    ledger, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    service.worker = MagicMock()
    event = {"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "903.112", "text": "I checked the belt and confirmed the issue.",
        "files": [{"id": "F1", "mimetype": "image/jpeg"}]}
    with patch.object(TicketQuests, "_download_jpeg", side_effect=requests.ConnectionError("temporary")):
        with pytest.raises(requests.RequestException):
            service.response_event(event, "mapping-change")
    source.data["slack_users"] = [row for row in source.data["slack_users"] if row["slack_id"] != "U2"]

    assert service.response_event(event, "mapping-change") is True
    saved = store.get("ledger_evidence", quest["_id"])
    response_id = "ticket-quest-response:" + sha1(f"CQUEST:{event['ts']}".encode()).hexdigest()
    assert "pending_claim" not in saved
    assert store.get("ledger_evidence", response_id)["rejection_reason"] == "claimant_identity_unavailable"
    assert ledger.participant(str(oid(2)))["xp"] == "0"


def test_reserved_image_retry_cannot_follow_reassigned_slack_identity(joined):
    ledger, store, source, service = setup_tickets(joined)
    ledger.join(str(oid(3)))
    quest = open_quest(service, store, source)
    service.worker = MagicMock()
    event = {"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "903.113", "text": "I checked the belt and confirmed the issue.",
        "files": [{"id": "F1", "mimetype": "image/jpeg"}]}
    with patch.object(TicketQuests, "_download_jpeg", side_effect=requests.ConnectionError("temporary")):
        with pytest.raises(requests.RequestException):
            service.response_event(event, "mapping-reassigned")
    source.data["slack_users"] = [
        {**row, "member_id": oid(3)} if row["slack_id"] == "U2" else row
        for row in source.data["slack_users"] if row["slack_id"] != "U3"]

    assert service.response_event(event, "mapping-reassigned") is True
    response_id = "ticket-quest-response:" + sha1(f"CQUEST:{event['ts']}".encode()).hexdigest()
    assert "pending_claim" not in store.get("ledger_evidence", quest["_id"])
    assert store.get("ledger_evidence", response_id)["rejection_reason"] == "claimant_identity_changed"
    assert ledger.participant(str(oid(2)))["xp"] == "0"
    assert ledger.participant(str(oid(3)))["xp"] == "0"


def test_reconcile_releases_claim_owned_by_terminal_slack_job(joined):
    ledger, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    service.worker = MagicMock()
    event = {"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "903.114", "text": "I checked the belt and confirmed the issue.",
        "files": [{"id": "F1", "mimetype": "image/jpeg"}]}
    owner_id = "slack:ticket-owner-failed"
    with patch.object(TicketQuests, "_download_jpeg", side_effect=requests.ConnectionError("temporary")):
        with pytest.raises(requests.RequestException):
            service.response_event(event, owner_id)
    store.put("ledger_inbox", {"_id": owner_id, "kind": "slack_event", "payload": event,
        "status": "failed", "attempts": 10})
    source.data["fix_tickets"][0]["status"] = "resolved"

    service.reconcile(ticket_id=735)

    saved = store.get("ledger_evidence", quest["_id"])
    response_id = "ticket-quest-response:" + sha1(f"CQUEST:{event['ts']}".encode()).hexdigest()
    assert "pending_claim" not in saved
    assert saved["status"] == "closed" and saved["outcome"] == "closed"
    assert store.get("ledger_evidence", response_id)["rejection_reason"] == "owner_job_failed"


def test_permanently_unavailable_jpeg_falls_back_to_no_image_xp(joined):
    ledger, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    service.worker = MagicMock()
    event = {"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "903.201", "text": "I checked the belt and confirmed the issue.",
        "files": [{"id": "deleted-file", "mimetype": "image/jpeg"}]}
    with patch.object(TicketQuests, "_download_jpeg", return_value=None):
        assert service.response_event(event, "permanent-image-error") is True
    assert ledger.participant(str(oid(2)))["xp"] == "66"
    response = store.select("ledger_evidence", {"kind": "broken_ticket_quest_response"})[0]
    assert response.get("image_file_id") is None


def test_decompression_bomb_rejects_claim_releases_reservation_and_narrates(joined):
    ledger, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    service.worker = MagicMock()
    unsafe = {"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "903.250", "text": "I checked the belt and confirmed the issue.",
        "files": [{"id": "huge-jpeg", "mimetype": "image/jpeg"}]}
    with patch.object(TicketQuests, "_download_jpeg", side_effect=requests.ConnectionError("retry")):
        with pytest.raises(requests.RequestException):
            service.response_event(unsafe, "decompression-bomb")
    ledger.join(str(oid(3)))
    later = {"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U3", "ts": "903.251", "text": "I checked the motor and confirmed the issue."}
    with pytest.raises(ReviewDeliveryBusy):
        service.response_event(later, "after-bomb")
    with patch.object(TicketQuests, "_download_jpeg", side_effect=Image.DecompressionBombError("too many pixels")):
        assert service.response_event(unsafe, "decompression-bomb") is True

    saved_quest = store.get("ledger_evidence", quest["_id"])
    rejected = store.select("ledger_evidence", {"kind": "broken_ticket_quest_response"})[0]
    assert saved_quest["status"] == "open"
    assert saved_quest.get("pending_claim") is None
    assert saved_quest.get("winner_member_id") is None
    assert rejected["claim_rejected"] is True
    assert rejected["rejection_reason"] == "unsafe_image_dimensions"
    assert rejected["eligible"] is False
    assert rejected.get("image_file_id") is None
    assert ledger.participant(str(oid(2)))["xp"] == "0"

    outbox = store.get("ledger_outbox", rejected["_id"])
    assert outbox["payload"]["winner"] is False
    assert not store.select("ledger_evidence", {"kind": "broken_ticket_quest_response", "slack_id": "U3"})
    assert service.response_event(later, "after-bomb") is True
    assert store.get("ledger_evidence", quest["_id"])["winner_member_id"] == str(oid(3))
    assert ledger.participant(str(oid(3)))["xp"] == "66"

    _, _, _, composer, _, slack = joined
    worker = Worker(ledger, composer, slack, bot_id="UBOT")
    worker.persist_composition = MagicMock(return_value={"text": "That image was too large for the Ledger's scanner."})
    response = store.get("ledger_evidence", rejected["_id"])
    response.update(delivery={"fix_channel": "CFIX"}, rails_note_written=True)
    store.put("ledger_evidence", response)
    service = TicketQuests(ledger, worker)
    service._deliver_response({"_id": rejected["_id"], "payload": {"response_id": rejected["_id"], "winner": False}})
    facts = worker.persist_composition.call_args.args[3]
    assert "snarky" in facts["summary"] and "Ledger" in facts["summary"]
    assert "too large" in slack.chat_postMessage.call_args.kwargs["text"]


def test_reserved_jpeg_retry_defers_while_paused_then_resumes(joined):
    ledger, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    service.worker = MagicMock()
    event = {"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "903.260", "text": "I checked the belt and confirmed the issue.",
        "files": [{"id": "F1", "mimetype": "image/jpeg"}]}
    with patch.object(TicketQuests, "_download_jpeg", side_effect=requests.Timeout("retry")):
        with pytest.raises(requests.RequestException):
            service.response_event(event, "pause-jpeg-retry", attempts=1)
    pending_before = store.get("ledger_evidence", quest["_id"])["pending_claim"]

    store.put("ledger_catalog", {"_id": "control", "paused": True})
    with patch.object(TicketQuests, "_download_jpeg") as download:
        with pytest.raises(ReviewDeliveryBusy):
            service.response_event(event, "pause-jpeg-retry", attempts=2)
        download.assert_not_called()
    assert store.get("ledger_evidence", quest["_id"])["pending_claim"] == pending_before
    assert not store.select("ledger_evidence", {"kind": "broken_ticket_quest_response"})

    store.put("ledger_catalog", {"_id": "control", "paused": False})
    with patch.object(TicketQuests, "_download_jpeg", return_value=b"sanitized"):
        assert service.response_event(event, "pause-jpeg-retry", attempts=2) is True
    assert ledger.participant(str(oid(2)))["xp"] == "100"
    assert not store.get("ledger_evidence", quest["_id"]).get("pending_claim")


def test_deleted_image_errors_are_permanent_but_auth_and_ratelimits_retry():
    slack_response = MagicMock()
    slack_response.get.side_effect = lambda key: "file_not_found" if key == "error" else None
    slack_response.status_code = 200
    missing = SlackApiError("missing", slack_response)
    assert TicketQuests._permanent_image_error(missing)

    slack_response.get.side_effect = lambda key: "ratelimited" if key == "error" else None
    ratelimited = SlackApiError("ratelimited", slack_response)
    assert not TicketQuests._permanent_image_error(ratelimited)

    forbidden_response = requests.Response()
    forbidden_response.status_code = 403
    forbidden = requests.HTTPError(response=forbidden_response)
    assert not TicketQuests._permanent_image_error(forbidden)

    retry_response = requests.Response()
    retry_response.status_code = 429
    retry_after = requests.HTTPError(response=retry_response)
    assert not TicketQuests._permanent_image_error(retry_after)


def test_jpeg_fetch_falls_back_on_permanent_errors_and_retries_transient_ones(joined):
    _, _, _, service = setup_tickets(joined)
    service.worker = MagicMock()
    response = MagicMock()
    response.get.side_effect = lambda key: "file_not_found" if key == "error" else None
    response.status_code = 200
    service.worker.slack.files_info.side_effect = SlackApiError("missing", response)
    assert service._download_jpeg({"image_file_id": "deleted"}) is None

    response.get.side_effect = lambda key: "ratelimited" if key == "error" else None
    service.worker.slack.files_info.side_effect = SlackApiError("ratelimited", response)
    with pytest.raises(SlackApiError):
        service._download_jpeg({"image_file_id": "temporary"})

    service.worker.slack.files_info.side_effect = None
    service.worker.slack.files_info.return_value = {"file": {
        "mimetype": "image/jpeg", "url_private_download": "https://files.slack.com/image"}}
    service.worker.slack.token = "test-token"
    for status, permanent in ((404, True), (503, False)):
        http_response = requests.Response()
        http_response.status_code = status
        failure = requests.HTTPError(response=http_response)
        result = MagicMock()
        result.__enter__.return_value = result
        result.headers = {}
        result.raise_for_status.side_effect = failure
        with patch("ledger.ticket_quests.requests.get", return_value=result):
            if permanent:
                assert service._download_jpeg({"image_file_id": "http-file"}) is None
            else:
                with pytest.raises(requests.HTTPError):
                    service._download_jpeg({"image_file_id": "http-file"})


def test_image_check_uses_no_image_award_after_retry_budget(joined):
    ledger, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    service.worker = MagicMock()
    event = {"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "903.301", "text": "I checked the belt and confirmed the issue.",
        "files": [{"id": "F1", "mimetype": "image/jpeg"}]}
    with patch.object(TicketQuests, "_download_jpeg", side_effect=requests.Timeout("offline")):
        assert service.response_event(event, "image-retries-exhausted", attempts=10) is True
    assert ledger.participant(str(oid(2)))["xp"] == "66"
    assert not store.get("ledger_evidence", quest["_id"]).get("pending_claim")


def test_ignores_nonthread_and_non_sentence_messages(joined):
    _, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    assert not service.response_event({"type": "message", "channel": "CQUEST", "user": "U2",
        "ts": "902.001", "text": "This is a sentence."}, "event")
    service.response_event({"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "902.002", "text": "I checked it"}, "event")
    assert store.get("ledger_evidence", quest["_id"]).get("winner_member_id") is None


def test_file_share_thread_reply_can_qualify_with_jpeg(joined):
    ledger, store, source, _ = setup_tickets(joined)
    quest = open_quest(TicketQuests(ledger), store, source)
    worker = Worker(ledger, joined[3], joined[5], bot_id="UBOT")
    with patch.object(TicketQuests, "_download_jpeg", return_value=b"sanitized-jpeg"):
        result = worker.event({"type": "message", "subtype": "file_share", "channel": "CQUEST",
            "thread_ts": quest["announcement_ts"], "user": "U2", "ts": "905.001",
            "text": "I checked the belt and confirmed the issue.",
            "files": [{"id": "F1", "mimetype": "image/jpeg"}]}, "file-share-event")
    assert result is True
    assert ledger.participant(str(oid(2)))["xp"] == "100"


def test_jpeg_download_stream_stops_at_limit_before_pillow(joined):
    ledger, _, _, _, _, slack = joined
    worker = Worker(ledger, joined[3], slack, bot_id="UBOT")
    slack.files_info.return_value = {"file": {"mimetype": "image/jpeg",
        "url_private_download": "https://files.slack.com/private/F1"}}
    worker.slack.token = "xoxb-test"

    class Response:
        headers = {}
        closed = False
        def __enter__(self):
            return self
        def __exit__(self, *_):
            self.closed = True
        def raise_for_status(self):
            pass
        def iter_content(self, chunk_size):
            assert chunk_size == 64 * 1024
            yield b"x" * (12 * 1024 * 1024)
            yield b"overflow"

    response = Response()
    with patch("ledger.ticket_quests.requests.get", return_value=response), \
         patch("ledger.ticket_quests.sanitize_jpeg") as sanitize:
        assert TicketQuests(ledger, worker)._download_jpeg({"image_file_id": "F1"}) is None
    sanitize.assert_not_called()
    assert response.closed


def test_image_filename_is_unique_for_each_response():
    quest = {"ticket_key": "735"}
    first = {"_id": "ticket-quest-response:abc123", "slack_id": "U2"}
    second = {"_id": "ticket-quest-response:def456", "slack_id": "U2"}
    assert TicketQuests._image_filename(quest, first) != TicketQuests._image_filename(quest, second)


@pytest.mark.parametrize("change", [("category", "other"), ("status", "awaiting_review")])
def test_quest_closes_when_ticket_becomes_ineligible(joined, change):
    _, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    source.data["fix_tickets"][0][change[0]] = change[1]
    service.reconcile(735)
    closed = store.get("ledger_evidence", quest["_id"])
    assert closed["status"] == "closed"
    assert closed["outcome"] == "ineligible"
    assert store.get("ledger_outbox", f"ticket-quest-update:{quest['_id']}:ineligible")


@pytest.mark.parametrize("targeted", [True, False])
def test_deleted_ticket_closes_quest_during_targeted_or_hourly_reconciliation(joined, targeted):
    _, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    source.data["fix_tickets"].clear()
    service.reconcile(735) if targeted else service.reconcile()
    closed = store.get("ledger_evidence", quest["_id"])
    assert closed["status"] == "closed"
    assert closed["outcome"] == "deleted"
    assert closed["final_ticket_status"] == "deleted"
    assert store.get("ledger_outbox", f"ticket-quest-update:{quest['_id']}:deleted")


def test_reply_to_deleted_ticket_closes_quest_instead_of_leaving_it_open(joined):
    ledger, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    source.data["fix_tickets"].clear()
    handled = service.response_event({"type": "message", "channel": "CQUEST",
        "thread_ts": quest["announcement_ts"], "user": "U2", "ts": "906.001",
        "text": "I checked the tool and confirmed the issue."}, "deleted-ticket-reply")
    assert handled is True
    closed = store.get("ledger_evidence", quest["_id"])
    assert closed["status"] == "closed" and closed["outcome"] == "deleted"
    assert ledger.participant(str(oid(2)))["xp"] == "0"


def test_ticket_quest_announcement_narration_uses_persisted_composition(joined):
    ledger, store, _, composer, _, slack = joined
    composer.compose = MagicMock()
    worker = Worker(ledger, composer, slack, bot_id="UBOT")
    worker.persist_composition = MagicMock(return_value={"text": "The quest has ended."})
    quest_id = ticket_key(735)
    store.put("ledger_evidence", {"_id": quest_id, "kind": "broken_ticket_quest", "ticket_key": "735",
        "status": "closed", "announcement_channel": "CQUEST", "announcement_ts": "900.001",
        "final_ticket_status": "resolved"})
    job = {"_id": "ticket-quest-update:735:closed", "payload": {"quest_id": quest_id, "outcome": "closed"}}
    TicketQuests(ledger, worker)._update_announcement(job)
    worker.persist_composition.assert_called_once()
    composer.compose.assert_not_called()
    assert slack.chat_update.call_args.kwargs["text"].startswith("The quest has ended.")


def test_response_thanks_uses_persisted_composition(joined):
    ledger, store, _, composer, _, slack = joined
    composer.compose = MagicMock()
    worker = Worker(ledger, composer, slack, bot_id="UBOT")
    worker.persist_composition = MagicMock(return_value={"text": "Thanks for the repair report."})
    response_id = "ticket-quest-response:abc123"
    quest_id = ticket_key(735)
    store.put("ledger_evidence", {"_id": quest_id, "kind": "broken_ticket_quest", "ticket_key": "735",
        "status": "won", "xp_awarded": 100})
    store.put("ledger_evidence", {"_id": response_id, "kind": "broken_ticket_quest_response", "quest_id": quest_id,
        "slack_id": "U2", "channel": "CQUEST", "thread_ts": "900.001", "text": "I checked the motor.",
        "delivery": {"fix_channel": "CFIX"}, "rails_note_written": True})
    service = TicketQuests(ledger, worker)
    service._deliver_response({"_id": response_id, "payload": {"response_id": response_id, "winner": False}})
    worker.persist_composition.assert_called_once()
    composer.compose.assert_not_called()
    assert slack.chat_postMessage.call_args.kwargs["text"] == "Thanks for the repair report."


def test_caption_failure_does_not_block_ticket_image_delivery(joined, monkeypatch):
    ledger, store, _, service = setup_tickets(joined)
    quest = open_quest(service, store, service.sources.data)
    _, _, _, composer, _, slack = joined
    worker = Worker(ledger, composer, slack, bot_id="UBOT")
    worker.persist_composition = MagicMock(return_value={"text": "Thanks for the report."})
    response_id = "ticket-quest-response:caption-outage"
    quest_id = quest["_id"]
    store.put("ledger_evidence", {"_id": response_id, "kind": "broken_ticket_quest_response", "quest_id": quest_id,
        "slack_id": "U2", "member_id": str(oid(2)), "channel": "CQUEST", "thread_ts": "900.001",
        "text": "I checked the switch.", "image_file_id": "F1", "delivery": {},
        "rails_note_written": True})
    slack.conversations_replies.return_value = {"messages": []}
    slack.files_upload_v2.return_value = {"files": [{"permalink": "https://files.example/image"}]}
    monkeypatch.setattr(TicketQuests, "_download_jpeg", lambda self, response: b"jpeg")
    monkeypatch.setattr(TicketQuests, "_upload_drive", lambda self, image, filename: None)
    monkeypatch.setattr("ledger.image_captioning.describe_image", lambda *args: (_ for _ in ()).throw(TimeoutError()))

    TicketQuests(ledger, worker)._deliver_response({"_id": response_id,
        "payload": {"response_id": response_id, "winner": False}})

    assert slack.chat_postMessage.call_count == 2  # Fix Tickets repost and participant acknowledgment
    upload = slack.files_upload_v2.call_args.kwargs["file_uploads"][0]
    assert "alt_txt" not in upload


def test_caption_event_is_separate_from_member_note():
    response = {"_id": "response-1", "text": "I inspected the switch.",
        "image_description": "A red switch."}
    quest = {"_id": "quest-1"}
    notes = TicketQuests._build_event_notes(735, quest, response, "https://files.example/image", 4,
        oid(2), oid(900), "UBOT")

    assert len(notes) == 2
    assert notes[0]["note"] == response["text"]
    assert notes[0]["actor_id"] == oid(2)
    assert notes[1]["note"] == "Image description (automatically generated): A red switch."
    assert notes[1]["actor_id"] is None and notes[1]["actor_slack_id"] == "UBOT"
    assert notes[1]["note_role"] == "ledger" and notes[1]["revision"] == notes[0]["revision"] + 1


@pytest.mark.parametrize("terminal", ["resolved", "rejected", "withdrawn"])
def test_terminal_ticket_closes_unanswered_quest_and_reopen_does_not_revive(joined, terminal):
    _, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    source.data["fix_tickets"][0]["status"] = terminal
    service.reconcile(735)
    assert store.get("ledger_evidence", quest["_id"])["status"] == "closed"
    source.data["fix_tickets"][0]["status"] = "open"
    service.reconcile(735)
    assert store.get("ledger_evidence", quest["_id"])["status"] == "closed"


def test_expiration_closes_without_winner(joined):
    _, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    quest["deadline"] = now() - timedelta(seconds=1)
    store.put("ledger_evidence", quest)
    service.reconcile(735)
    assert store.get("ledger_evidence", quest["_id"])["status"] == "expired"


def test_jpeg_sanitizer_strips_exif_and_rejects_non_jpeg():
    image = Image.new("RGB", (4, 3), "red")
    exif = Image.Exif()
    exif[270] = "private camera note"
    source = BytesIO()
    image.save(source, format="JPEG", exif=exif)
    result = sanitize_jpeg(source.getvalue())
    assert Image.open(BytesIO(result)).getexif() == {}
    png = BytesIO()
    image.save(png, format="PNG")
    assert sanitize_jpeg(png.getvalue()) is None


def test_jpeg_sanitizer_rejects_excessive_dimensions_before_transform():
    source = MagicMock(format="JPEG", width=5000, height=5000)
    with patch("ledger.ticket_quests.Image.open", return_value=source), \
         patch("ledger.ticket_quests.ImageOps.exif_transpose") as transpose:
        assert sanitize_jpeg(b"jpeg") is None
    transpose.assert_not_called()


def test_config_save_records_revision_and_audit(joined):
    ledger, store, *_ = joined
    service = TicketQuests(ledger)
    saved = service.save_config(str(oid(10)), {"quest_channel_id": "CQUEST", "fix_channel_id": "CFIX",
        "duration_hours": 24, "xp_with_image": 120, "xp_without_image": 70})
    assert config(ledger)["revision"] == 1
    assert saved["enabled"] is True
    assert store.get("ledger_evidence", "broken-ticket-quest-config:1")["actor"] == str(oid(10))
