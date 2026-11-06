"""Durable, action-scoped private results; facts and authorization stay in Python.

Owners and immutable delivery revisions use existing owned collections. Collection
callbacks perform no network requests, and every receipt merges the current owner
instead of replacing events that arrived while Slack or inference was running.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from uuid import NAMESPACE_URL, uuid5

from slack_sdk.errors import SlackApiError

from .domain import Denied, Ledger
from .messages import button, escape, section
from .storage import enqueue, now

WINDOW_SECONDS = 60
LOCK_SECONDS = 120
TERMINAL = {"delivered", "failed", "cancelled"}


class SummaryPending(RuntimeError):
    """A fixed aggregation deadline is pending; defer without spending attempts."""
    def __init__(self, delay=15):
        self.delay = max(1, int(delay))
        super().__init__("Summary aggregation is pending.")


class SummaryBusy(SummaryPending):
    """Another delivery owns the summary lease; defer without spending attempts."""


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _owner_id(action_id, member_id, authorization, generation=None):
    return "notification-summary:" + _fingerprint([str(action_id), str(member_id), authorization, generation])


def _owner(ledger, action_id, member_id, authorization, identity):
    generation = ((ledger.participant(member_id) or {}).get("consent_generation", 0)
                  if authorization == "game" else None)
    key = _owner_id(action_id, member_id, authorization, generation)
    stamp = now()
    return ledger.store.get("ledger_evidence", key) or {
        "_id": key, "kind": "notification_summary", "action_id": str(action_id),
        "member_id": str(member_id), "authorization": authorization,
        "consent_generation": generation, "identity": identity,
        "at": stamp, "deadline": stamp + timedelta(seconds=WINDOW_SECONDS),
        "status": "collecting", "events": [], "event_ids": [], "content_revision": 0,
        "complete": False, "post_count": 0, "update_count": 0}


def _schedule(store, owner, immediate=False):
    key = f"summary-flush:{owner['_id']}:{owner['content_revision']}"
    delay = 0 if immediate else max(0, math.ceil((owner["deadline"] - now()).total_seconds()))
    enqueue(store, "ledger_outbox", key, "summary_flush",
            {"summary_id": owner["_id"], "member_id": owner["member_id"],
             "exception": owner["authorization"] == "peer_kudos",
             "peer_kudos": owner["authorization"] == "peer_kudos"}, delay=delay)


def collect(ledger, action_id, member_id, kind, facts, event_id):
    """Record one authorized game event. Event IDs make repeats harmless."""
    if not ledger.active(member_id):
        return None
    identity = ledger.sources.slack_id(member_id)
    generation = (ledger.participant(member_id) or {}).get("consent_generation", 0)
    def write(store):
        current = Ledger(store, ledger.sources)
        participant = current.participant(member_id) or {}
        if not participant.get("opted_in") or participant.get("consent_generation", 0) != generation:
            return None
        owner = _owner(current, action_id, member_id, "game", identity)
        if str(event_id) in owner["event_ids"]:
            return owner["_id"]
        owner["event_ids"].append(str(event_id))
        owner["events"].append({"event_id": str(event_id), "type": kind, "facts": deepcopy(facts)})
        owner["content_revision"] += 1
        store.put("ledger_evidence", owner)
        _schedule(store, owner, immediate=owner["complete"])
        return owner["_id"]
    return ledger.store.atomic(write)


def finish_action(ledger, action_id):
    """Release this transaction's game results, separately for every recipient."""
    def write(store):
        current = Ledger(store, ledger.sources)
        for owner in store.select("ledger_evidence", {"kind": "notification_summary",
                "action_id": str(action_id), "authorization": "game"}):
            participant = current.participant(owner["member_id"]) or {}
            # An old action can never be completed under a new consent generation.
            if participant.get("consent_generation", 0) != owner["consent_generation"]:
                continue
            total = participant.get("xp", "0")
            if not owner["complete"] or owner.get("xp_total") != total:
                owner.update(complete=True, xp_total=total, completed_at=now(),
                             content_revision=owner["content_revision"] + 1)
                store.put("ledger_evidence", owner)
            _schedule(store, owner, immediate=True)
    return ledger.store.atomic(write)


def _kudos_status(evidence):
    destinations = ["recipient"] + (["shared"] if evidence.get("public") else [])
    statuses = {key: evidence.get("deliveries", {}).get(key, {}).get("status", "pending")
                for key in destinations}
    complete = all(value in TERMINAL for value in statuses.values())
    successful = all(value == "delivered" for value in statuses.values())
    return statuses, complete, successful


def track_kudos(ledger, evidence):
    """Collect the giver's receipt; never include authored text or recipient progress."""
    identity = ledger.sources.slack_id(evidence["giver"])
    def write(store):
        current = Ledger(store, ledger.sources)
        live = store.get("ledger_evidence", evidence["_id"]) or evidence
        owner = _owner(current, live["_id"], live["giver"], "peer_kudos", identity)
        statuses, complete, _ = _kudos_status(live)
        signature = _fingerprint([statuses, bool(live.get("xp_awarded")), bool(live.get("public"))])
        if owner.get("kudos_signature") != signature:
            owner.update(kudos_id=live["_id"], kudos_signature=signature, complete=complete,
                         content_revision=owner["content_revision"] + 1)
            store.put("ledger_evidence", owner)
            _schedule(store, owner, immediate=complete)
        return owner["_id"]
    return ledger.store.atomic(write)


def _decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else Decimal(0)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _number(value):
    return format(value.normalize(), "f")


def _kudos_facts(evidence, recipient_identity):
    statuses, complete, successful = _kudos_status(evidence)
    uid, name = recipient_identity
    recipient = f"<@{uid}>" if uid and re.fullmatch(r"[UW][A-Z0-9]+", uid) else "the recipient"
    if successful:
        text = f"Your kudos reached {recipient} in DM" + (" and Ledge Chat." if "shared" in statuses else ".")
    elif not any(status == "delivered" for status in statuses.values()):
        text = f"Your kudos to {recipient} is still pending." if not complete else f"Your kudos to {recipient} could not be delivered."
    else:
        delivered = "DM" if statuses["recipient"] == "delivered" else "Ledge Chat"
        text = f"Your kudos reached {recipient} in {delivered}."
    if not successful:
        labels = {"recipient": "DM", "shared": "Ledge Chat"}
        # Include the exact outcome of every unfinished/failed destination.
        details = [f"{labels[key]}: {value}." for key, value in statuses.items() if value != "delivered"]
        text += " " + " ".join(details)
    xp_result = "17 XP awarded" if evidence.get("xp_awarded") else "0 XP"
    text += " 17 XP was awarded." if evidence.get("xp_awarded") else " No XP was awarded."
    outcome = ("delivered" if successful else "partial" if "delivered" in statuses.values() else
               "pending" if not complete else "failed" if "failed" in statuses.values() else "cancelled")
    return {"summary": text, "successful": successful, "complete": complete,
            "recipient_slack_id": uid, "recipient_full_name": name or None,
            "delivery_status": outcome,
            "dm_status": statuses["recipient"], "public_status": statuses.get("shared", "not requested"),
            "xp_result": xp_result}


def _label(value):
    """Bound escaped display labels so Slack never truncates a factual line."""
    value = str(value).replace("\n", " ").strip()
    escaped = escape(value)
    if len(escaped) <= 180:
        return escaped
    clipped = escaped[:179]
    if clipped.rfind("&") > clipped.rfind(";"):
        clipped = clipped[:clipped.rfind("&")]
    return clipped + "…"


def _game_facts(owner):
    awarded, lines, seen = {}, [], set()
    named_milestones = {event["type"] for event in owner["events"]
                        if event["type"] in ("boss", "stewardship") and
                        (event["facts"].get("quest_title") or event["facts"].get("challenge_title"))}
    for event in owner["events"]:
        facts, kind = event["facts"], event["type"]
        award_id = str(facts.get("award_id") or event["event_id"])
        delta = _decimal(facts.get("xp_change", 0))
        if delta > 0:
            awarded.setdefault(award_id, delta)
        if kind == "rank_up":
            line = "Rank reached: " + _label(facts.get("rank", "your next rank")) + "."
        elif kind == "shop_complete":
            line = "Shop completed: " + _label(facts.get("shop", "Shop")) + "."
        elif kind == "boss":
            title = facts.get("quest_title") or facts.get("challenge_title")
            line = ("Boss milestone completed" + (": " + _label(title) if title else "") + "."
                    if title or kind not in named_milestones else "")
        elif kind == "stewardship":
            title = facts.get("quest_title") or facts.get("challenge_title")
            line = ("Stewardship milestone completed" + (": " + _label(title) if title else "") + "."
                    if title or kind not in named_milestones else "")
        elif facts.get("challenge_title"):
            line = "Verified: " + _label(facts["challenge_title"]) + "."
        elif facts.get("quest_title"):
            line = "Quest completed: " + _label(facts["quest_title"]) + "."
        elif facts.get("summary"):
            line = _label(facts["summary"])
        elif kind in ("quest", "challenge", "first_build", "develop_mentor", "mentoring"):
            label = facts.get("quest_title") or facts.get("challenge") or kind.replace("_", " ").capitalize()
            line = "Recorded: " + _label(label) + "."
        elif facts.get("tool_name") or facts.get("tool"):
            line = "Tool skill recorded: " + _label(facts.get("tool_name") or facts["tool"]) + "."
        elif facts.get("volunteer_title") or facts.get("title"):
            line = "Recorded: " + _label(facts.get("volunteer_title") or facts["title"]) + "."
        else:
            line = ""
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    omitted = max(0, len(lines) - 10)
    lines = lines[:10]
    if omitted:
        lines.append(f"{omitted} additional updates; use /ledger achievements for details.")
    total = sum(awarded.values(), Decimal(0))
    verified = [event for event in owner["events"] if event["facts"].get("verified_milestone")]
    xp_known = total > 0 or owner["complete"] and bool(verified) and all(
        event["facts"].get("xp_outcome_known") for event in verified)
    if total > 0:
        lines.insert(0, "You earned " + _number(total) + " XP.")
    elif xp_known:
        lines.insert(0, "No XP was awarded.")
    if not lines:
        lines = ["Your progress has been recorded."]
    return {"summary": "\n".join(lines), "successful": True, "complete": owner["complete"],
            "xp_change": _number(total), "xp_total": owner.get("xp_total"),
            "xp_outcome_known": bool(xp_known),
            "achievements": [{"type": event["type"], **deepcopy(event["facts"])} for event in owner["events"][:10]],
            "additional_updates": omitted}


def flush(worker, job):
    """Freeze the latest authoritative results into one immutable delivery revision."""
    baseline = worker.store.get("ledger_evidence", job["payload"]["summary_id"])
    if not baseline:
        return
    sources = worker.ledger.sources
    def identity(member_id):
        member = sources.member(member_id) or {}
        return sources.slack_id(member_id), " ".join(str(member.get(k) or "").strip() for k in ("firstname", "lastname")).strip() or None
    member_uid, member_name = identity(baseline["member_id"])
    recipient_identity = (None, None)
    if baseline["authorization"] == "peer_kudos":
        baseline_evidence = worker.store.get("ledger_evidence", baseline["kudos_id"])
        if not baseline_evidence:
            return
        recipient_identity = identity(baseline_evidence["recipient"])
    def write(store):
        worker_job = store.get("ledger_outbox", job["_id"])
        if not worker_job or worker_job.get("lease") != job.get("lease") or worker_job["status"] != "working":
            raise Denied("Summary job lease changed.")
        owner = store.get("ledger_evidence", job["payload"]["summary_id"])
        if not owner:
            return
        if owner["content_revision"] != baseline["content_revision"]:
            raise SummaryBusy(1)
        if owner["authorization"] == "peer_kudos":
            evidence = store.get("ledger_evidence", owner["kudos_id"])
            if not evidence:
                return
            facts = _kudos_facts(evidence, recipient_identity)
        else:
            facts = _game_facts(owner)
        if not facts["complete"] and now() < owner["deadline"]:
            raise SummaryPending(math.ceil((owner["deadline"] - now()).total_seconds()))
        facts.update(member_slack_id=member_uid, member_full_name=member_name)
        fingerprint = _fingerprint(facts)
        revision = owner["content_revision"]
        if owner.get("snapshot_revision") == revision:
            return
        if fingerprint == owner.get("delivered_fingerprint"):
            owner["delivered_revision"] = revision
            store.put("ledger_evidence", owner)
            return
        key = f"summary-delivery:{owner['_id']}:{revision}"
        payload = {"summary_id": owner["_id"], "member_id": owner["member_id"],
                   "revision": revision, "fingerprint": fingerprint, "facts_snapshot": facts,
                   "authorization": owner["authorization"], "consent_generation": owner["consent_generation"],
                   "type": "delivery" if owner["authorization"] == "peer_kudos" else "status",
                   "audience": "member", "exception": owner["authorization"] == "peer_kudos",
                   "peer_kudos": owner["authorization"] == "peer_kudos"}
        enqueue(store, "ledger_outbox", key, "summary_delivery", payload)
        owner.update(snapshot_fingerprint=fingerprint, snapshot_revision=revision, snapshot_job_id=key)
        store.put("ledger_evidence", owner)
    return worker.store.atomic(write)


def _authorized(worker, owner):
    member_id = owner["member_id"]
    def check():
        if owner["authorization"] == "peer_kudos":
            # Receipt jobs are explicit pause exceptions, but still require the
            # same live, permitted human identity as peer kudos itself.
            if not worker.ledger.member_eligible(member_id):
                raise Denied("A valid linked human Slack account is required.")
        else:
            participant = worker.ledger.require(member_id)
            if participant.get("consent_generation", 0) != owner["consent_generation"]:
                raise Denied("Summary belongs to an earlier consent generation.")
    check()
    uid = worker.valid_identity(member_id)
    check()  # Identity lookup is external; consent may change while it runs.
    if not uid or uid != owner.get("identity"):
        raise Denied("Summary identity changed or became inactive.")
    return uid


def _blocks(text, owner):
    chunks = []
    for line in text.splitlines():
        if chunks and len(chunks[-1]) + len(line) < 2900:
            chunks[-1] += "\n" + line
        else:
            chunks.append(line)
    blocks = [section(chunk) for chunk in chunks]
    if owner["authorization"] == "game":
        blocks.append({"type": "actions", "elements": [button("Suggest a next step", "guidance_next_step", owner["_id"])]})
    return blocks


def deliver(worker, job):
    """Post once, then edit the same parent as immutable later revisions arrive."""
    payload, token = job["payload"], job["lease"]

    def acquire(store):
        current = store.get("ledger_outbox", job["_id"])
        if not current or current["status"] != "working" or current.get("lease") != token:
            raise Denied("Summary delivery lease changed.")
        owner = store.get("ledger_evidence", payload["summary_id"])
        if not owner or owner.get("delivered_revision", -1) >= payload["revision"]:
            return None
        if owner.get("snapshot_revision") != payload["revision"] or owner["content_revision"] != payload["revision"]:
            return None  # A newer flush is responsible for all accumulated facts.
        if owner.get("delivery_lock_until", now()) > now() and owner.get("delivery_lock") != token:
            raise SummaryBusy()
        owner.update(delivery_lock=token, delivery_lock_until=now() + timedelta(seconds=LOCK_SECONDS))
        store.put("ledger_evidence", owner)
        return owner

    owner = worker.store.atomic(acquire)
    if not owner:
        return

    def release(store):
        latest = store.get("ledger_evidence", owner["_id"])
        if latest and latest.get("delivery_lock") == token:
            latest.pop("delivery_lock", None)
            latest.pop("delivery_lock_until", None)
            store.put("ledger_evidence", latest)

    try:
        uid = _authorized(worker, owner)
        current = worker.store.get("ledger_outbox", job["_id"])
        composition = current.get("composed") or {}
        text = current.get("rendered_text")
        if text is None:
            facts = payload["facts_snapshot"]
            narration = ""
            if facts["complete"] and facts["successful"]:
                composed = worker.persist_composition(job, payload["type"], "member", facts,
                    profile="receipt" if owner["authorization"] == "peer_kudos" else "summary")
                composition = composed
                if composed.get("outcome") == "generated":
                    narration = composed.get("text", "").strip()
            separator = " " if owner["authorization"] == "peer_kudos" else "\n"
            text = facts["summary"] + ((separator + narration) if narration else "")
            def save(store):
                saved = store.get("ledger_outbox", job["_id"])
                if saved.get("lease") != token or saved["status"] != "working":
                    raise Denied("Summary composition lease changed.")
                saved["rendered_text"] = text
                store.put("ledger_outbox", saved)
            worker.store.atomic(save)
        worker.assert_live_job(job)
        latest = worker.store.get("ledger_evidence", owner["_id"])
        _authorized(worker, latest)
        latest = worker.store.get("ledger_evidence", owner["_id"])
        if latest.get("delivery_lock") != token or latest.get("delivery_lock_until", now()) <= now():
            raise SummaryBusy()
        if latest["content_revision"] != payload["revision"] or latest.get("snapshot_revision") != payload["revision"]:
            return  # Events arriving during inference are delivered by their own latest flush.
        blocks = _blocks(text, latest)
        channel = latest.get("channel") or worker.slack.conversations_open(users=uid)["channel"]["id"]
        worker.assert_live_job(job)
        latest = worker.store.get("ledger_evidence", owner["_id"])
        _authorized(worker, latest)
        latest = worker.store.get("ledger_evidence", owner["_id"])
        if latest.get("delivery_lock") != token or latest.get("delivery_lock_until", now()) <= now():
            raise SummaryBusy()
        if latest["content_revision"] != payload["revision"] or latest.get("snapshot_revision") != payload["revision"]:
            return
        posted, updated = False, False

        def reserve_post(store, replacement=False):
            current = store.get("ledger_evidence", owner["_id"])
            if current.get("delivery_lock") != token:
                raise SummaryBusy()
            if replacement:
                address = current["ts"]
                if current.get("replacement_for") != address:
                    current.update(replacement_for=address,
                        replacement_token=str(uuid5(NAMESPACE_URL, owner["_id"] + ":replacement:" + address)),
                        replacement_revision=payload["revision"])
                post_token, reserved_revision = current["replacement_token"], current["replacement_revision"]
            else:
                current.setdefault("post_token", str(uuid5(NAMESPACE_URL, owner["_id"])))
                current.setdefault("post_reserved_revision", payload["revision"])
                post_token, reserved_revision = current["post_token"], current["post_reserved_revision"]
            store.put("ledger_evidence", current)
            return post_token, reserved_revision

        def confirmed_ts(response):
            ts = response.get("ts")
            if not isinstance(ts, str) or not ts:
                raise RuntimeError("Slack did not confirm a summary message address.")
            return ts

        def post_parent(replacement=False):
            post_token, reserved_revision = worker.store.atomic(lambda s: reserve_post(s, replacement))
            response = worker.post_message(channel=channel, text=text, blocks=blocks,
                client_msg_id=post_token, unfurl_links=False, unfurl_media=False)
            ts = confirmed_ts(response)
            def save_address(store):
                current_job = store.get("ledger_outbox", job["_id"])
                live = store.get("ledger_evidence", owner["_id"])
                if current_job.get("lease") != token or current_job["status"] != "working" or live.get("delivery_lock") != token:
                    raise SummaryBusy()
                # Save the confirmed address before any corrective update, so a
                # failed update can retry against its parent instead of reposting.
                live.update(channel=channel, ts=ts, post_count=live.get("post_count", 0) + 1)
                live.setdefault("posted_at", now())
                store.put("ledger_evidence", live)
            worker.store.atomic(save_address)
            recovered = reserved_revision != payload["revision"]
            if recovered:
                worker.assert_live_job(job)
                _authorized(worker, worker.store.get("ledger_evidence", owner["_id"]))
                # Slack may deduplicate the stable token to a prior uncertain
                # post's older text. Explicitly update it to this frozen revision.
                response = worker.slack.chat_update(channel=channel, ts=ts, text=text, blocks=blocks,
                                                    unfurl_links=False, unfurl_media=False)
                ts = confirmed_ts(response)
            return ts, recovered

        if latest.get("ts"):
            try:
                response = worker.slack.chat_update(channel=channel, ts=latest["ts"], text=text, blocks=blocks,
                                                    unfurl_links=False, unfurl_media=False)
                ts, updated = confirmed_ts(response), True
            except SlackApiError as exc:
                if exc.response.get("error") != "message_not_found":
                    raise
                ts, updated = post_parent(replacement=True)
                posted = True
        else:
            ts, updated = post_parent()
            posted = True
        def receipt(store):
            saved = store.get("ledger_outbox", job["_id"])
            live = store.get("ledger_evidence", owner["_id"])
            if saved.get("lease") != token or saved["status"] != "working" or live.get("delivery_lock") != token:
                raise SummaryBusy()
            # Do not replace live events, content revision, or its newer flush address.
            stamp = now()
            live.update(channel=channel, ts=ts, delivered_revision=payload["revision"],
                        delivered_fingerprint=payload["fingerprint"], status="delivered", delivered_at=stamp)
            if updated:
                live["update_count"] = live.get("update_count", 0) + 1
            live.setdefault("posted_at", stamp)
            saved["delivery_metrics"] = {"operation": "post_and_update" if posted and updated else "update" if updated else "post",
                "queue_age_ms": max(0, int((stamp - saved.get("created_at", owner["at"])).total_seconds() * 1000)),
                "summary_latency_ms": max(0, int((stamp - owner["at"]).total_seconds() * 1000)),
                "generation_ms": (saved.get("composed") or composition).get("generation_ms", 0),
                "fallback_reason": (saved.get("composed") or composition).get("fallback_reason"),
                "post_count": live["post_count"], "update_count": live["update_count"]}
            store.put("ledger_evidence", live)
            store.put("ledger_outbox", saved)
        worker.store.atomic(receipt)
    finally:
        worker.store.atomic(release)
