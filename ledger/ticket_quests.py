"""Opt-in broken-tool verification quests backed by existing Ledger records."""
from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha1
from io import BytesIO
import logging
import json
import os
import re
from uuid import NAMESPACE_URL, uuid5

import requests
from bson import ObjectId
from PIL import Image, ImageOps
from pymongo.errors import PyMongoError
from slack_sdk.errors import SlackApiError

from .domain import Denied, Ledger
from .messages import section
from .sources import object_id, sid
from .storage import enqueue, now

log = logging.getLogger(__name__)

CONFIG_ID = "broken_ticket_quests"
TERMINAL = {"resolved", "rejected", "withdrawn"}
ACTIVE = {"open", "in_progress", "waiting_for_parts"}
VALID_CATEGORIES = {"damaged", "broken"}
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
DEFAULT_CONFIG = {"enabled": False, "quest_channel_id": "", "fix_channel_id": "",
                  "duration_hours": 18, "xp_with_image": 100, "xp_without_image": 66,
                  "revision": 0}
SENTENCE = re.compile(r"\S.{0,3997}[.!?](?:[\"')\]]*)?(?:\s|$)", re.S)


def config(ledger):
    row = ledger.store.get("ledger_catalog", CONFIG_ID) or {}
    return {**DEFAULT_CONFIG, **row}


def validate_config(values):
    result = dict(values)
    for key in ("quest_channel_id", "fix_channel_id"):
        value = result.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"[CG][A-Z0-9]+", value):
            raise ValueError(f"Enter a valid Slack channel ID for {key.replace('_', ' ')}.")
    for key, low, high in (("duration_hours", 1, 168), ("xp_with_image", 0, 500), ("xp_without_image", 0, 500)):
        value = result.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key.replace('_', ' ').title()} must be between {low} and {high}.")
    if type(result.get("enabled", True)) is not bool:
        raise ValueError("Quest enablement must be selected explicitly.")
    result["enabled"] = result.get("enabled", True)
    return result


def canonical_id(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, str) and ObjectId.is_valid(value):
        return str(ObjectId(value))
    if type(value) is int and 0 < value <= 9223372036854775807:
        return str(value)
    if isinstance(value, str) and value.isdigit() and 0 < int(value) <= 9223372036854775807:
        return str(int(value))
    raise ValueError("Ticket ID is not a positive sequence ID or legacy ObjectId.")


def ticket_key(value):
    return f"broken-ticket-quest:{canonical_id(value)}"


def sanitize_jpeg(content):
    if not isinstance(content, bytes) or len(content) > 12 * 1024 * 1024:
        return None
    source = Image.open(BytesIO(content))
    if source.format not in ("JPEG", "JPG"):
        return None
    if source.width * source.height > MAX_IMAGE_PIXELS:
        return None
    image = ImageOps.exif_transpose(source).convert("RGB")
    output = BytesIO()
    image.save(output, format="JPEG", quality=92, optimize=True)
    return output.getvalue()


class TicketQuests:
    def __init__(self, ledger, worker=None):
        self.ledger, self.store, self.sources, self.worker = ledger, ledger.store, ledger.sources, worker

    def save_config(self, actor, values):
        self.ledger.admin(actor)
        checked = validate_config(values)
        current = config(self.ledger)
        revision = current.get("revision", 0) + 1
        current_time = now()
        checked.update(_id=CONFIG_ID, revision=revision, updated_at=current_time, updated_by=actor,
                       enabled_at=current.get("enabled_at") if current.get("enabled") else current_time)
        audit_id = f"broken-ticket-quest-config:{revision}"
        def write(s):
            previous = s.get("ledger_catalog", CONFIG_ID) or {}
            if previous.get("revision", 0) != current.get("revision", 0):
                raise ValueError("Quest settings changed while this form was open. Reopen the settings form.")
            s.put("ledger_catalog", checked)
            s.put("ledger_evidence", {"_id": audit_id, "kind": "ticket_quest_config_audit", "actor": actor,
                "revision": revision, "settings": {k: checked[k] for k in DEFAULT_CONFIG}, "at": now()})
        self.store.atomic(write)
        return checked

    def _ticket(self, key):
        ident = object_id(key)
        rows = self.sources.rows("fix_tickets", {"_id": ident})
        if rows:
            return rows[0]
        # Native integer IDs remain integers; BSON ObjectIds are not coerced to strings.
        if isinstance(ident, str) and ident.isdigit():
            rows = self.sources.rows("fix_tickets", {"_id": int(ident)})
        return rows[0] if rows else None

    @staticmethod
    def _ticket_ineligible_reason(ticket):
        if ticket is None:
            return "deleted"
        if ticket.get("status") in TERMINAL:
            return "closed"
        if ticket.get("status") not in ACTIVE or ticket.get("category") not in VALID_CATEGORIES:
            return "ineligible"
        return None

    def _close_claim_for_ineligible_ticket(self, quest, ticket, channel, root):
        reason = self._ticket_ineligible_reason(ticket)
        if not reason:
            return False
        pending = quest.get("pending_claim") or {}
        if pending.get("response_id"):
            self._release_stale_claim(quest["_id"], pending["response_id"], pending,
                channel, root, "ticket_" + reason)
        self._refresh_quest(ticket, config(self.ledger), now(), ticket_id=quest["ticket_id"])
        return True

    def reconcile(self, ticket_id=None):
        settings = config(self.ledger)
        current = now()
        if ticket_id is not None:
            try:
                ticket = self._ticket(ticket_id)
            except (ValueError, PyMongoError):
                return
            if ticket and settings.get("enabled"):
                self._consider(ticket, settings)
            self._refresh_quest(ticket, settings, current, ticket_id=ticket_id)
            return

        # Use a durable watermark to recover MQTT events after outages. The first
        # enabled scan starts now, avoiding retroactive quests for old repairs.
        if settings.get("enabled"):
            cursor = self.store.get("ledger_catalog", "broken_ticket_quest_scan") or {}
            start = cursor.get("updated_after") or settings.get("enabled_at", current)
            tickets = self.sources.rows("fix_tickets", {"updated_at": {"$gte": start}})
            for ticket in tickets:
                self._consider(ticket, settings)
            # Event history also recovers category/status changes during an MQTT outage.
            events = self.sources.rows("fix_ticket_events", {"completed_at": {"$gte": start}})
            for event in events:
                if event.get("kind") not in ("created", "updated", "note"):
                    continue
                ticket = self._ticket(event.get("ticket_id"))
                if ticket:
                    self._consider(ticket, settings)
                    self._refresh_quest(ticket, settings, current)
        quests = self.store.select("ledger_evidence", {"kind": "broken_ticket_quest", "status": {"$in": ["announcing", "open"]}})
        for quest in quests:
            ticket = self._ticket(quest["ticket_id"])
            self._refresh_quest(ticket, settings, current, ticket_id=quest["ticket_id"])
        if settings.get("enabled"):
            self.store.atomic(lambda s: s.put("ledger_catalog", {"_id": "broken_ticket_quest_scan", "updated_after": current}))

    def _consider(self, ticket, settings):
        try:
            ident = canonical_id(ticket.get("_id"))
        except ValueError:
            return
        created = ticket.get("created_at")
        if (ticket.get("category") not in VALID_CATEGORIES or ticket.get("status") not in ACTIVE
                or not isinstance(created, datetime)
                or (settings.get("enabled_at") and created < settings["enabled_at"])):
            return
        quest_id = ticket_key(ticket["_id"])
        if self.store.get("ledger_evidence", quest_id):
            return
        reporter = sid(ticket.get("reporter_id"))
        doc = {"_id": quest_id, "kind": "broken_ticket_quest", "ticket_id": ticket["_id"],
               "ticket_key": ident, "reporter_id": reporter, "status": "announcing",
               "category": ticket["category"], "created_at": now(), "deadline": None,
               "announcement_channel": settings["quest_channel_id"], "announcement_ts": None,
               "announcement_delivery": None, "winner_member_id": None, "responses": 0}
        def reserve(s):
            if s.get("ledger_evidence", quest_id):
                return False
            s.put("ledger_evidence", doc)
            enqueue(s, "ledger_outbox", f"ticket-quest-announcement:{ident}", "ticket_quest_announce", {"quest_id": quest_id})
            return True
        self.store.atomic(reserve)

    def _refresh_quest(self, ticket, settings, current, *, ticket_id=None):
        if ticket is None and ticket_id is None:
            return
        key = ticket_key(ticket["_id"] if ticket is not None else ticket_id)
        quest = self.store.get("ledger_evidence", key)
        if not quest or quest.get("status") not in ("announcing", "open") or quest.get("winner_member_id"):
            return
        pending = quest.get("pending_claim") or {}
        if pending:
            owner_job = (self.store.get("ledger_inbox", pending.get("inbox_job_id"))
                         if pending.get("inbox_job_id") else None)
            if owner_job is None and pending.get("message_ts"):
                # Older reservations predate inbox_job_id; recover their owner
                # from the retained Slack event payload when possible.
                owners = self.store.select("ledger_inbox", {"kind": "slack_event",
                    "status": {"$in": ["failed", "cancelled"]},
                    "payload.channel": quest.get("announcement_channel"),
                    "payload.ts": pending["message_ts"]}, limit=1)
                owner_job = owners[0] if owners else None
            if (pending.get("response_id") and owner_job and
                    owner_job.get("status") in ("failed", "cancelled")):
                self._release_stale_claim(key, pending.get("response_id"), pending,
                    quest.get("announcement_channel"), quest.get("announcement_ts"), "owner_job_failed")
                quest = self.store.get("ledger_evidence", key) or quest
                pending = quest.get("pending_claim") or {}
            if pending:
                return
        eligible = ticket is not None and ticket.get("status") in ACTIVE and ticket.get("category") in VALID_CATEGORIES
        outcome = ("deleted" if ticket is None else
                   "closed" if ticket.get("status") in TERMINAL else
                   "ineligible" if not eligible else None)
        final_status = ticket.get("status") if ticket is not None else "deleted"
        final_category = ticket.get("category") if ticket is not None else quest.get("category")
        if quest.get("status") == "announcing":
            if not outcome:
                return
            def cancel_announcement(s):
                latest = s.get("ledger_evidence", key)
                if not latest or latest.get("status") != "announcing":
                    return
                latest.update(status="closed", outcome_at=current, outcome=outcome,
                              final_ticket_status=final_status, final_ticket_category=final_category)
                s.put("ledger_evidence", latest)
                pending = s.get("ledger_outbox", f"ticket-quest-announcement:{latest['ticket_key']}")
                if pending and pending.get("status") in ("pending", "working"):
                    pending["status"] = "cancelled"
                    s.put("ledger_outbox", pending)
            self.store.atomic(cancel_announcement)
            return
        if not outcome and quest.get("deadline") and quest["deadline"] <= current:
            outcome = "expired"
        if not outcome:
            return
        def close(s):
            latest = s.get("ledger_evidence", key)
            if not latest or latest.get("status") != "open" or latest.get("winner_member_id"):
                return False
            latest.update(status="expired" if outcome == "expired" else "closed", outcome_at=current,
                          outcome=outcome, final_ticket_status=final_status,
                          final_ticket_category=final_category)
            s.put("ledger_evidence", latest)
            enqueue(s, "ledger_outbox", f"ticket-quest-update:{key}:{outcome}", "ticket_quest_update", {"quest_id": key, "outcome": outcome})
            return True
        self.store.atomic(close)

    def response_event(self, event, event_key, attempts=1):
        channel, root = event.get("channel"), event.get("thread_ts")
        if not channel or not root or not event.get("user") or not event.get("ts"):
            return False
        quests = self.store.select("ledger_evidence", {"kind": "broken_ticket_quest", "announcement_channel": channel,
            "announcement_ts": root, "status": {"$in": ["open", "won", "closed", "expired"]}})
        if not quests:
            pending = self.store.select("ledger_evidence", {"kind": "broken_ticket_quest", "announcement_channel": channel,
                "announcement_ts": None, "status": "announcing"}, limit=1)
            if pending:
                from .review_notifications import ReviewDeliveryBusy
                raise ReviewDeliveryBusy()
            return False
        quest = quests[0]
        response_id = "ticket-quest-response:" + sha1(f"{channel}:{event['ts']}".encode()).hexdigest()
        if self.store.get("ledger_evidence", response_id):
            return True
        pending = quest.get("pending_claim") or {}
        owns_pending = pending.get("response_id") == response_id
        if (self.store.get("ledger_catalog", "control") or {}).get("paused"):
            if owns_pending or pending:
                from .review_notifications import ReviewDeliveryBusy
                raise ReviewDeliveryBusy()
            return True
        if quest.get("status") == "open":
            ticket = self._ticket(quest["ticket_id"])
            if self._close_claim_for_ineligible_ticket(quest, ticket, channel, root):
                return True
        member = self.sources.identity(event["user"])
        if not member:
            if owns_pending:
                self._release_stale_claim(quest["_id"], response_id, pending, channel, root,
                                          "claimant_identity_unavailable")
            return True
        member_id = sid(member["_id"])
        participant = self.ledger.participant(member_id)
        if owns_pending and pending.get("member_id") != member_id:
            self._release_stale_claim(quest["_id"], response_id, pending, channel, root,
                                      "claimant_identity_changed")
            return True
        if owns_pending and (not participant or
                pending.get("consent_generation") != participant.get("consent_generation", 0)):
            self._release_stale_claim(quest["_id"], response_id, pending, channel, root,
                                      "consent_generation_changed")
            return True
        text = event.get("text", "")
        if len(text) > 4000:
            text = text[:4000]
        files = event.get("files") or []
        jpeg = next((f for f in files if isinstance(f, dict) and f.get("mimetype") in ("image/jpeg", "image/jpg")
                     and f.get("id")), None)
        eligible = (member_id != quest.get("reporter_id") and self.ledger.active(member_id)
                    and self.ledger.sources.good_standing(member_id)
                    and self._sentence(text))
        if eligible and pending and not owns_pending and quest.get("status") == "open":
            # Keep the inbox event pending. Once the reserved JPEG claim is
            # accepted or released, this response can be checked for the win.
            from .review_notifications import ReviewDeliveryBusy
            raise ReviewDeliveryBusy()
        config_now = config(self.ledger)
        if owns_pending and not jpeg and pending.get("image_file_id"):
            jpeg = {"id": pending["image_file_id"], "mimetype": "image/jpeg"}
        if eligible and jpeg and not owns_pending:
            def reserve_claim(s):
                latest = s.get("ledger_evidence", quest["_id"])
                if not latest or latest.get("status") != "open" or latest.get("winner_member_id"):
                    return False
                current_member = Ledger(s, self.sources)
                participant = current_member.participant(member_id)
                current_time = now()
                if (latest.get("pending_claim") or not latest.get("deadline") or latest["deadline"] <= current_time
                        or not participant or not participant.get("opted_in") or not current_member.active(member_id)
                        or not self.sources.good_standing(member_id) or member_id == latest.get("reporter_id")):
                    return False
                latest["pending_claim"] = {"response_id": response_id, "member_id": member_id,
                    "slack_id": event["user"], "message_ts": event["ts"], "claimed_at": current_time,
                    "consent_generation": participant.get("consent_generation", 0),
                    "inbox_job_id": event_key,
                    "image_file_id": jpeg["id"], "text": text,
                    "xp_with_image": config_now["xp_with_image"], "xp_without_image": config_now["xp_without_image"]}
                s.put("ledger_evidence", latest)
                return True
            owns_pending = self.store.atomic(reserve_claim)

        image_valid = False
        claim_rejected = False
        if jpeg and owns_pending and self.worker:
            try:
                image_valid = bool(self._download_jpeg({"image_file_id": jpeg["id"]}))
            except (requests.RequestException, PyMongoError, SlackApiError) as exc:
                # Reserve the first eligible JPEG claimant before I/O. Earlier
                # transient failures retry without letting later replies win.
                # After the inbox retry budget, resolve the reservation as a
                # text-only claim so it cannot block the quest indefinitely.
                if attempts < 10:
                    raise
                log.warning("ticket quest image check exhausted response=%s error_type=%s",
                            response_id, type(exc).__name__)
                jpeg = None
            except Image.DecompressionBombError:
                # Pillow rejects the decoded image before the explicit pixel
                # limit can run. Reject this claim and release its reservation.
                claim_rejected = True
                jpeg = None
            except (ValueError, OSError):
                jpeg = None
        elif jpeg and owns_pending and not self.worker:
            # Tests and non-delivery callers have no Slack client to verify the
            # file; preserve the legacy image award behavior for that path.
            image_valid = True

        if jpeg and owns_pending and self.worker and not image_valid:
            jpeg = None

        # Image verification can take long enough for the source ticket to
        # change after the initial retry check. Re-read it at the acceptance
        # boundary, and release any reserved claim before closing the quest.
        latest_quest = self.store.get("ledger_evidence", quest["_id"]) or quest
        if latest_quest.get("status") == "open":
            ticket = self._ticket(latest_quest["ticket_id"])
            if self._close_claim_for_ineligible_ticket(latest_quest, ticket, channel, root):
                return True

        record = {"_id": response_id, "kind": "broken_ticket_quest_response", "quest_id": quest["_id"],
                  "member_id": member_id, "slack_id": event["user"], "channel": channel, "thread_ts": root,
                  "message_ts": event["ts"], "text": text,
                  "image_file_id": jpeg.get("id") if jpeg else None,
                  "eligible": bool(eligible), "claim_rejected": claim_rejected,
                  "rejection_reason": "unsafe_image_dimensions" if claim_rejected else None,
                  "created_at": now(), "delivery": None}
        winner = False
        def accept(s):
            nonlocal winner
            if s.get("ledger_evidence", response_id):
                return
            latest = s.get("ledger_evidence", quest["_id"])
            current_member = Ledger(s, self.sources)
            participant = current_member.participant(member_id)
            claim = (latest or {}).get("pending_claim") or {}
            if (claim.get("response_id") == response_id and
                    claim.get("member_id") != member_id):
                self._release_stale_claim_in_transaction(s, quest["_id"], response_id, claim,
                    channel, root, "claimant_identity_changed")
                return
            owns_claim = (claim.get("response_id") == response_id and
                          claim.get("member_id") == member_id)
            claim_generation_matches = (not owns_claim or claim.get("consent_generation") ==
                                        (participant or {}).get("consent_generation", 0))
            if (eligible and latest and latest.get("status") == "open" and claim and not owns_claim):
                from .review_notifications import ReviewDeliveryBusy
                raise ReviewDeliveryBusy()
            deadline_ok = bool(latest and latest.get("deadline") and (
                claim.get("claimed_at") <= latest["deadline"] if owns_claim else latest["deadline"] > now()))
            valid = (eligible and not claim_rejected and latest and latest.get("status") == "open"
                     and not latest.get("winner_member_id")
                     and (not claim or owns_claim) and deadline_ok and claim_generation_matches
                     and participant and participant.get("opted_in") and current_member.active(member_id)
                     and self.sources.good_standing(member_id) and member_id != latest.get("reporter_id"))
            record["eligible"] = bool(valid)
            if valid:
                amount = (claim.get("xp_with_image", config_now["xp_with_image"]) if image_valid and jpeg
                          else claim.get("xp_without_image", config_now["xp_without_image"]) if owns_claim
                          else config_now["xp_with_image"] if jpeg else config_now["xp_without_image"])
                changed = current_member.award(member_id, latest["_id"], str(amount), "broken_ticket_quest", historical=True,
                                               facts={"ticket_id": latest["ticket_key"]})
                if changed:
                    current_member._advance(member_id, historical=True)
                latest.update(status="won", winner_member_id=member_id, winner_response_id=response_id,
                              winner_at=now(), xp_awarded=amount)
                winner = True
            if owns_claim:
                latest.pop("pending_claim", None)
            latest["responses"] = latest.get("responses", 0) + 1
            s.put("ledger_evidence", latest)
            s.put("ledger_evidence", record)
            if winner:
                enqueue(s, "ledger_outbox", f"ticket-quest-update:{latest['_id']}:won", "ticket_quest_update",
                        {"quest_id": latest["_id"], "outcome": "won"})
            enqueue(s, "ledger_outbox", response_id, "ticket_quest_response", {"response_id": response_id,
                "winner": winner, "quest_id": quest["_id"]})
        self.store.atomic(accept)
        return True

    def _release_stale_claim(self, quest_id, response_id, pending, channel, root, reason):
        """Release a reservation and tombstone its response so replay cannot re-claim it."""
        return self.store.atomic(lambda s: self._release_stale_claim_in_transaction(
            s, quest_id, response_id, pending, channel, root, reason))

    @staticmethod
    def _release_stale_claim_in_transaction(store, quest_id, response_id, pending, channel, root, reason):
        latest = store.get("ledger_evidence", quest_id)
        claim = (latest or {}).get("pending_claim") or {}
        if (not latest or claim.get("response_id") != response_id or
                claim.get("member_id") != pending.get("member_id")):
            return False
        latest.pop("pending_claim", None)
        latest["responses"] = latest.get("responses", 0) + 1
        response = {"_id": response_id, "kind": "broken_ticket_quest_response",
            "quest_id": quest_id, "member_id": pending["member_id"],
            "slack_id": pending.get("slack_id"), "channel": channel, "thread_ts": root,
            "message_ts": pending.get("message_ts"), "text": pending.get("text", ""),
            "image_file_id": None, "eligible": False, "claim_rejected": False,
            "rejection_reason": reason, "created_at": now(), "delivery": None}
        store.put("ledger_evidence", latest)
        if not store.get("ledger_evidence", response_id):
            store.put("ledger_evidence", response)
        return True

    @staticmethod
    def _sentence(text):
        return isinstance(text, str) and bool(text.strip()) and bool(SENTENCE.search(text.strip()))

    def deliver(self, job):
        if not self.worker:
            raise RuntimeError("Ticket quest delivery requires a worker")
        kind, payload = job["kind"], job["payload"]
        if kind == "ticket_quest_announce":
            return self._announce(job)
        if kind == "ticket_quest_update":
            return self._update_announcement(job)
        if kind == "ticket_quest_response":
            return self._deliver_response(job)
        raise ValueError("Unknown ticket quest delivery")

    def _announce(self, job):
        quest = self.store.get("ledger_evidence", job["payload"]["quest_id"])
        if not quest or quest.get("status") != "announcing":
            return
        if quest.get("announcement_ts"):
            return
        hours = config(self.ledger)["duration_hours"]
        text = (f"*Broken tool verification quest — ticket #{quest['ticket_key']}*\n"
                f"A {quest['category']} report needs attention. Be the first eligible participant to reply here with a complete sentence describing what you did to verify or help resolve it.\n"
                f"The quest ends {hours} hours after this post or when the ticket is resolved, rejected, or withdrawn. The reporter cannot claim it.")
        response = self.worker.post_message(channel=quest["announcement_channel"], text=text,
            client_msg_id=str(uuid5(NAMESPACE_URL, job["_id"])), unfurl_links=False, unfurl_media=False)
        def save(s):
            latest = s.get("ledger_evidence", quest["_id"])
            if latest and not latest.get("announcement_ts") and latest.get("status") == "announcing":
                latest.update(status="open", announcement_ts=response["ts"], announcement_delivery={"channel": response.get("channel", quest["announcement_channel"]), "ts": response["ts"]},
                              started_at=now(), deadline=now() + timedelta(hours=hours), duration_hours=hours)
                s.put("ledger_evidence", latest)
            elif latest and latest.get("status") == "closed" and not latest.get("announcement_ts"):
                latest.update(announcement_ts=response["ts"], announcement_delivery={"channel": response.get("channel", quest["announcement_channel"]), "ts": response["ts"]})
                s.put("ledger_evidence", latest)
                outcome = latest.get("outcome", "closed")
                enqueue(s, "ledger_outbox", f"ticket-quest-update:{quest['_id']}:{outcome}", "ticket_quest_update",
                        {"quest_id": quest["_id"], "outcome": outcome})
        self.store.atomic(save)

    def _update_announcement(self, job):
        quest = self.store.get("ledger_evidence", job["payload"]["quest_id"])
        if not quest or not quest.get("announcement_ts"):
            return
        if quest.get("status") == "won":
            text = f"Quest complete: <@{self.sources.slack_id(quest['winner_member_id']) or ''}> was first to help with ticket #{quest['ticket_key']}."
        elif job["payload"]["outcome"] == "closed":
            facts = {"summary": f"The ticket verification quest ended without a winner because ticket #{quest['ticket_key']} is now {quest.get('final_ticket_status', 'closed')}."}
            narration = self.worker.persist_composition(job, "status", "shared", facts).get("text", "")
            text = (narration + "\n" if narration else "") + facts["summary"]
        elif job["payload"]["outcome"] == "ineligible":
            facts = {"summary": (f"The verification quest for ticket #{quest['ticket_key']} closed because it is no longer an eligible "
                                  f"damaged/broken ticket (category: {quest.get('final_ticket_category') or 'unknown'}, "
                                  f"status: {quest.get('final_ticket_status') or 'unknown'}).")}
            narration = self.worker.persist_composition(job, "status", "shared", facts).get("text", "")
            text = (narration + "\n" if narration else "") + facts["summary"]
        elif job["payload"]["outcome"] == "deleted":
            facts = {"summary": f"The verification quest for ticket #{quest['ticket_key']} closed because the source ticket was deleted."}
            narration = self.worker.persist_composition(job, "status", "shared", facts).get("text", "")
            text = (narration + "\n" if narration else "") + facts["summary"]
        else:
            facts = {"summary": f"The verification window for ticket #{quest['ticket_key']} expired without a qualifying response."}
            narration = self.worker.persist_composition(job, "status", "shared", facts).get("text", "")
            text = (narration + "\n" if narration else "") + facts["summary"]
        self.worker.slack.chat_update(channel=quest["announcement_channel"], ts=quest["announcement_ts"], text=text, blocks=[section(text)])

    @staticmethod
    def _image_filename(quest, response):
        response_suffix = response["_id"].rsplit(":", 1)[-1]
        return f"quest-{quest['ticket_key']}-{response['slack_id']}-{response_suffix}.jpg"

    def _download_jpeg(self, response):
        if not response.get("image_file_id"):
            return None
        try:
            info = self.worker.slack.files_info(file=response["image_file_id"])["file"]
        except SlackApiError as exc:
            if self._permanent_image_error(exc):
                return None
            raise
        if info.get("mimetype") not in ("image/jpeg", "image/jpg") or not info.get("url_private_download"):
            return None
        url = info["url_private_download"]
        if not url.startswith("https://files.slack.com/"):
            return None
        token = getattr(self.worker.slack, "token", None)
        if not token:
            return None
        try:
            with requests.get(url, headers={"Authorization": "Bearer " + token}, timeout=(2, 10),
                              allow_redirects=False, stream=True) as result:
                result.raise_for_status()
                size = result.headers.get("Content-Length")
                if size:
                    try:
                        if int(size) > MAX_IMAGE_BYTES:
                            return None
                    except (TypeError, ValueError):
                        pass
                content = bytearray()
                for chunk in result.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    if len(content) + len(chunk) > MAX_IMAGE_BYTES:
                        return None
                    content.extend(chunk)
                return sanitize_jpeg(bytes(content))
        except requests.HTTPError as exc:
            if self._permanent_image_error(exc):
                return None
            raise

    @staticmethod
    def _permanent_image_error(exc):
        # Private-file 401/403 responses can reflect repairable workspace auth
        # or scope configuration, so let the claim retry before XP fallback.
        transient_http = {401, 403, 408, 425, 429}
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(exc, requests.HTTPError) and isinstance(status, int):
            return 400 <= status < 500 and status not in transient_http
        if isinstance(exc, SlackApiError):
            error = exc.response.get("error")
            # Only errors that identify the requested file itself justify an
            # immediate text-only award. Workspace auth, scope, membership,
            # and unknown API errors may recover, so leave them to the retry
            # budget even when Slack reports a client-error HTTP status.
            return error in {"file_not_found", "file_deleted", "file_access_denied", "not_found", "not_shared"}
        return False

    def _upload_drive(self, image, filename):
        token = os.environ.get("LEDGER_TICKET_GOOGLE_ACCESS_TOKEN")
        folder = os.environ.get("LEDGER_TICKET_GOOGLE_FOLDER_ID")
        if not token or not folder or not image:
            return None
        # Google Drive has no idempotency key for uploads. A deterministic
        # filename/folder lookup lets outbox retries reuse a prior accepted file.
        lookup = requests.get("https://www.googleapis.com/drive/v3/files",
            headers={"Authorization": "Bearer " + token}, params={
                "q": f"name = '{filename}' and '{folder}' in parents and trashed = false",
                "pageSize": 1, "fields": "files(id,webViewLink)"}, timeout=(2, 10))
        lookup.raise_for_status()
        existing = (lookup.json().get("files") or [])
        if existing:
            row = existing[0]
            return row.get("webViewLink") or f"https://drive.google.com/file/d/{row['id']}/view"
        metadata = {"name": filename, "parents": [folder], "mimeType": "image/jpeg"}
        result = requests.post("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,webViewLink",
            headers={"Authorization": "Bearer " + token}, files={
                "metadata": ("metadata", json.dumps(metadata), "application/json; charset=UTF-8"),
                "file": (filename, image, "image/jpeg")}, timeout=(2, 15))
        result.raise_for_status()
        data = result.json()
        return data.get("webViewLink") or (f"https://drive.google.com/file/d/{data['id']}/view" if data.get("id") else None)

    def _deliver_response(self, job):
        response = self.store.get("ledger_evidence", job["payload"]["response_id"])
        if not response:
            return
        quest = self.store.get("ledger_evidence", response["quest_id"])
        if not quest:
            return
        image = self._download_jpeg(response)
        filename = self._image_filename(quest, response)
        try:
            drive_url = self._upload_drive(image, filename) if image else None
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.info("ticket quest Drive upload unavailable response=%s error_type=%s", response["_id"], type(exc).__name__)
            drive_url = None
        image_url = drive_url
        receipt = response.get("delivery") or {}
        if not receipt.get("fix_channel"):
            settings = config(self.ledger)
            fix_channel = settings.get("fix_channel_id")
            thread = None
            ticket = self._ticket(quest["ticket_id"])
            if (ticket and ticket.get("slack_ticket_ts") and ticket.get("slack_ticket_channel_id") == fix_channel):
                try:
                    replies = self.worker.slack.conversations_replies(channel=fix_channel, ts=ticket["slack_ticket_ts"], limit=1)
                    if replies.get("messages") and replies["messages"][0].get("ts") == ticket["slack_ticket_ts"]:
                        thread = ticket["slack_ticket_ts"]
                except Exception as exc:
                    # The configured channel still receives a new root post.
                    log.info("ticket Slack root unavailable ticket=%s error_type=%s", quest["ticket_key"], type(exc).__name__)
            text = f"<@{response['slack_id']}> reported quest progress for ticket #{quest['ticket_key']}:\n{response['text']}"
            kwargs = {"channel": fix_channel, "text": text,
                      "client_msg_id": str(uuid5(NAMESPACE_URL, job["_id"])), "unfurl_links": False, "unfurl_media": False}
            if thread:
                kwargs["thread_ts"] = thread
            posted = self.worker.post_message(**kwargs)
            receipt.update(fix_channel=fix_channel, fix_ts=posted["ts"], fix_thread=thread)
            if image:
                file_thread = thread or posted["ts"]
                files = self._thread_file_list(fix_channel, file_thread)
                found = next((f for f in files if f.get("name") == filename), None)
                if not found:
                    uploaded = self.worker.slack.files_upload_v2(file_uploads=[{"file": BytesIO(image), "filename": filename,
                        "title": filename}], channel=fix_channel, thread_ts=file_thread)
                    found = next(iter(uploaded.get("files") or []), None)
                if found:
                    image_url = drive_url or found.get("permalink")
            def save(s):
                current = s.get("ledger_evidence", response["_id"])
                current["delivery"] = {**(current.get("delivery") or {}), **receipt}
                current["image_url"] = image_url
                s.put("ledger_evidence", current)
            self.store.atomic(save)
        image_url = image_url or response.get("image_url")
        self._write_event_note(quest, response, image_url)
        if response.get("claim_rejected"):
            summary = ("Tell the participant their oversized JPEG claim was rejected because the image exceeded safe "
                       "dimensions. Give a brief, snarky but kind Ledger-style quip, and invite a smaller image or a "
                       "text-only sentence. Make clear this reply did not win or earn XP.")
            fallback = ("That JPEG tried to smuggle a whole galaxy past the image gate. Claim rejected; try a smaller "
                        "image or a text-only sentence.")
        elif job["payload"].get("winner"):
            summary = "Thank the responder for being first to qualify for the ticket verification quest."
            fallback = "You were first to qualify for this quest."
        elif response.get("eligible"):
            summary = "Thank the responder for helping with the ticket verification; another eligible participant qualified first."
            fallback = "Thanks for helping. Another eligible participant qualified first."
        else:
            summary = "Thank the responder for helping with ticket verification."
            fallback = "Thanks for helping with the ticket verification."
        narration = self.worker.persist_composition(job, "status", "shared", {"summary": summary}).get("text", "")
        message = narration or fallback
        if job["payload"].get("winner") and not response.get("claim_rejected"):
            message += f" Recorded {quest.get('xp_awarded', 0)} XP."
        self.worker.post_message(channel=response["channel"], thread_ts=response["thread_ts"], text=message,
            client_msg_id=str(uuid5(NAMESPACE_URL, job["_id"] + ":thanks")), unfurl_links=False)

    def _write_event_note(self, quest, response, image_url):
        """Best-effort source event write; Ledger/Slack are authoritative if denied."""
        if response.get("rails_note_written"):
            return
        try:
            actor = object_id(response["member_id"])
            ticket_id = object_id(quest["ticket_id"])
            note_id = ObjectId(sha1((quest["_id"] + ":" + response["_id"]).encode()).digest()[:12])
            db = self.sources.db
            client = db.client
            with client.start_session() as session:
                def transaction(s):
                    existing = db.fix_ticket_events.find_one({"_id": note_id}, session=s)
                    if existing:
                        return existing.get("revision")
                    ticket = db.fix_tickets.find_one({"_id": ticket_id}, session=s)
                    if not ticket and isinstance(ticket_id, str) and ticket_id.isdigit():
                        ticket = db.fix_tickets.find_one({"_id": int(ticket_id)}, session=s)
                    if not ticket:
                        raise ValueError("Ticket no longer exists")
                    revision = int(ticket.get("revision", 0))
                    changed = db.fix_tickets.update_one({"_id": ticket["_id"], "revision": revision},
                        {"$inc": {"revision": 1}, "$set": {"updated_at": now()}}, session=s)
                    if changed.modified_count != 1:
                        raise RuntimeError("Ticket revision changed concurrently")
                    note = {"_id": note_id, "ticket_id": ticket["_id"], "actor_id": actor,
                        "kind": "note", "note": response["text"], "image_url": image_url,
                        "revision": revision + 1, "created_at": now(), "field_changes": {}, "recipients": [],
                        "unscoped_staff_notification": False, "central_enabled": False, "delivered": {},
                        "delivery_attempts": {}, "completed_at": now()}
                    if image_url is None:
                        note.pop("image_url")
                    db.fix_ticket_events.insert_one(note, session=s)
                    return revision + 1
                session.with_transaction(transaction)
            self.store.atomic(lambda s: self._mark_note_written(s, response["_id"]))
        except (PyMongoError, ValueError, TypeError, RuntimeError, AttributeError, KeyError) as exc:
            log.info("ticket quest note skipped response=%s error_type=%s", response["_id"], type(exc).__name__)

    def _thread_file_list(self, channel, thread_ts):
        try:
            messages = self.worker.slack.conversations_replies(channel=channel, ts=thread_ts, limit=100).get("messages", [])
        except Exception as exc:
            log.info("ticket Slack file receipt lookup unavailable error_type=%s", type(exc).__name__)
            return []
        return [file for message in messages for file in (message.get("files") or []) if isinstance(file, dict)]

    @staticmethod
    def _mark_note_written(store, response_id):
        row = store.get("ledger_evidence", response_id)
        if row:
            row["rails_note_written"] = True
            store.put("ledger_evidence", row)
