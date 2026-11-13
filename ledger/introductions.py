"""Once-ever introductions. Python owns consent, timing and all delivery decisions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import re
from uuid import NAMESPACE_URL, uuid4, uuid5

from slack_sdk.errors import SlackApiError

from .domain import Denied, Ledger
from .messages import button, section
from .sources import object_id, sid
from .storage import enqueue, now

log = logging.getLogger(__name__)
COLLECTION = "ledger_relationships"
CONTROL = "automatic-introductions"
WINDOW = timedelta(days=14)
PAGE_SIZE = 100
TERMINAL = {"joined", "declined", "expired", "suppressed"}
QUESTION = "Hi! Do you enjoy role-playing games?"
CLARIFICATION = (
    "I mean games where you develop a character and build skills over time. "
    "The Ledger brings that idea to learning and helping at the makerspace. "
    "Would you like to hear more? Saying yes does not sign you up.")
EXPLANATION = (
    "The Ledger is an optional makerspace role-playing game. Verified learning, mentoring, "
    "and community contributions earn XP and ranks. Joining imports your verified makerspace "
    "history and invites you to private game channels. You can leave at any time. "
    "Participation never changes tool safety clearances or membership status. "
    "Review the consent form to choose whether to participate.")
TEXT = {"question": QUESTION, "clarification": CLARIFICATION, "explanation": EXPLANATION}


def enabled():
    return os.environ.get("LEDGER_AUTO_INTRODUCTIONS_ENABLED", "false").lower() in ("true", "1", "yes")


def key(member_id):
    return "automatic-introduction:" + str(member_id)


def record(store, member_id):
    return store.get(COLLECTION, key(member_id))


def _new(member_id):
    return {"_id": key(member_id), "kind": "automatic_introduction", "member_id": str(member_id),
            "status": "watching", "revision": 0, "ever_nonfacilities": False, "messages": {}}


def _save(store, row):
    row["revision"] = row.get("revision", 0) + 1
    store.put(COLLECTION, row)


def _maintenance(store, row, delay=0):
    if delay >= WINDOW.total_seconds() and row.get("deadline"):
        due = _date(row["deadline"])
        suffix = "deadline:" + str(due.timestamp())
        delay = max(0, (due - now()).total_seconds())
    elif delay:
        # All scans/retries converge on one poll per member per 15-minute bucket.
        # Otherwise every reconciliation would start another recurring job chain.
        due = datetime.fromtimestamp((int(now().timestamp()) // 900 + 1) * 900, timezone.utc)
        suffix = "poll:" + str(int(due.timestamp()))
        delay = (due - now()).total_seconds()
    else:
        suffix = "change:" + str(row.get("revision", 0))
    enqueue(store, "ledger_outbox", f"intro-maintain:{row['member_id']}:{suffix}",
            "intro_maintain", {"member_id": row["member_id"]}, delay=delay)


def _needs_maintenance(row):
    return bool(row.get("reserved_at") and (row["status"] not in TERMINAL or any(
        m["state"] in ("posting", "unknown") or (m.get("ts") and not m.get("cleaned_at"))
        for m in row.get("messages", {}).values())))


def _close(store, row, status):
    if row["status"] not in TERMINAL:
        row.update(status=status, closed_at=now())
    _save(store, row)
    _maintenance(store, row)


def eligible(ledger, member_id, slack_id=None):
    member = ledger.sources.member(member_id)
    uid = ledger.sources.slack_id(member_id)
    identity = ledger.store.get("ledger_catalog", f"identity:{member_id}") or {}
    from .ineligible import is_ineligible_slack_id
    return bool(member and member.get("status") == "activeMember" and uid
                and (slack_id is None or uid == slack_id) and not is_ineligible_slack_id(uid)
                and not ledger.participant(member_id) and not identity.get("bot")
                and not identity.get("deactivated") and uid != "USLACKBOT")


def _running(ledger):
    control = ledger.store.get("ledger_catalog", CONTROL) or {}
    return (enabled() and control.get("phase") == "active"
            and not (ledger.store.get("ledger_catalog", "control") or {}).get("paused"))


def _date(value):
    # Unknown/imported formats cannot prove a future first checkout.
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _facilities(sources):
    configured = os.environ.get("LEDGER_FACILITIES_SHOP_ID", "").strip()
    if configured:
        rows = sources.bounded("shops", {"_id": object_id(configured)}, ["name"], 1)
        if len(rows) != 1:
            raise ValueError("LEDGER_FACILITIES_SHOP_ID does not identify a source shop")
        return sid(rows[0]["_id"])
    matches, cursor = [], None
    while True:
        query = {"_id": {"$gt": cursor}} if cursor else {}
        page = sources.bounded("shops", query, ["name"], PAGE_SIZE)
        matches.extend(sid(s["_id"]) for s in page if str(s.get("name", "")).strip().casefold() == "facilities")
        if len(page) < PAGE_SIZE:
            break
        cursor = page[-1]["_id"]
    if len(matches) != 1:
        raise ValueError("Automatic introductions require one unambiguous Facilities shop")
    return matches[0]


def schedule_scan(ledger, trigger):
    if not enabled():
        def disable(store):
            control = store.get("ledger_catalog", CONTROL)
            if control and control.get("phase") != "disabled":
                control["phase"] = "disabled"
                store.put("ledger_catalog", control)
        ledger.store.atomic(disable)
        return
    ledger.store.atomic(lambda s: enqueue(s, "ledger_inbox", "intro-scan:" + trigger, "intro_scan", {}))


def scan(ledger, job):
    """One bounded member page; baseline and continuation checkpoints survive crashes."""
    if not enabled():
        schedule_scan(ledger, job["_id"])
        return
    control = ledger.store.get("ledger_catalog", CONTROL)
    if not control or control.get("phase") == "disabled":
        facilities = _facilities(ledger.sources)
        def start(s):
            current = s.get("ledger_catalog", CONTROL)
            if not current or current.get("phase") == "disabled":
                current = {"_id": CONTROL, "kind": "automatic_introduction_control", "phase": "baseline",
                           "generation": str(uuid4()), "facilities_id": facilities, "baseline_started_at": now()}
                s.put("ledger_catalog", current)
            return current
        control = ledger.store.atomic(start)
    payload = job["payload"]
    if payload.get("generation") and (payload["generation"] != control["generation"]
            or payload["baseline"] != (control["phase"] == "baseline")):
        return
    baseline = control["phase"] == "baseline"
    cursor = payload.get("cursor")
    query = {"_id": {"$gt": object_id(cursor)}} if cursor else {}
    members = ledger.sources.bounded("members", query, ["status", "merged_at"], PAGE_SIZE)
    for member in members:
        observe(ledger, sid(member["_id"]), baseline=baseline)
    def advance(s):
        current = s.get("ledger_catalog", CONTROL)
        if not current or current["generation"] != control["generation"] or current["phase"] != control["phase"]:
            return
        if len(members) == PAGE_SIZE:
            next_cursor = sid(members[-1]["_id"])
            root = payload.get("root", job["_id"])
            enqueue(s, "ledger_inbox", root + ":" + next_cursor, "intro_scan",
                    {"root": root, "cursor": next_cursor, "generation": control["generation"], "baseline": baseline})
        elif baseline:
            current.update(phase="active", activated_at=now())
            s.put("ledger_catalog", current)
    ledger.store.atomic(advance)


def observe(ledger, member_id, *, baseline=False):
    """Refresh after revision conflicts; never apply a stale source snapshot on retry."""
    control = ledger.store.get("ledger_catalog", CONTROL) or {}
    if not enabled() or control.get("phase") not in ("baseline", "active"):
        return
    if baseline and control["phase"] != "baseline":
        return  # An old baseline worker must not consume post-activation changes.
    for _ in range(4):
        previous = record(ledger.store, member_id) or _new(member_id)
        if previous.get("reserved_at") or previous["status"] == "joined":
            if _needs_maintenance(previous):
                ledger.store.atomic(lambda s: _maintenance(s, previous))
            return
        member = ledger.sources.member(member_id)
        checkouts = ledger.sources.bounded("tool_checkouts", {"member_id": object_id(member_id)},
                                          ["tool_id", "checked_out_at", "revoked_at"], 1001)
        if len(checkouts) > 1000:
            raise ValueError("Introduction checkout history exceeds its bounded read")
        tools = ledger.sources.tools_by_id([c.get("tool_id") for c in checkouts], ["shop_id"])
        shops = ledger.sources.shops_by_id([t.get("shop_id") for t in tools.values()], ["name"])
        outside, unknown = [], False
        for checkout in checkouts:
            tool = tools.get(sid(checkout.get("tool_id"))) or {}
            shop = sid(tool.get("shop_id"))
            if shop not in shops:
                unknown = True
            elif shop != control["facilities_id"]:
                outside.append(checkout)
        dates = [_date(c.get("checked_out_at")) for c in outside]
        cutoff = _date(control.get("activated_at"))
        first = (not previous.get("ever_nonfacilities") and not unknown and bool(outside)
                 and all(d is not None for d in dates) and cutoff is not None
                 and cutoff <= min(dates) <= now()
                 and any(not c.get("revoked_at") for c in outside))
        renewal = previous.get("observed_status") == "expired" and member and member.get("status") == "activeMember"
        uid = ledger.sources.slack_id(member_id)
        qualifies = bool(not baseline and control["phase"] == "active" and (first or renewal)
                         and eligible(ledger, member_id, uid))
        def commit(s):
            row = record(s, member_id) or _new(member_id)
            current_control = s.get("ledger_catalog", CONTROL) or {}
            if row["revision"] != previous["revision"]:
                return False
            if (current_control.get("generation"), current_control.get("phase")) != (control["generation"], control["phase"]):
                return True
            row.update(observed_status=(member or {}).get("status"), observed_at=now(),
                       ever_nonfacilities=bool(row.get("ever_nonfacilities") or outside or unknown))
            identity_key = "introduction-identity:" + str(uid)
            if qualifies and not s.get("ledger_participants", member_id) and not s.get(COLLECTION, identity_key):
                # Preserve suppression when a source merge replaces the member ID.
                s.put(COLLECTION, {"_id": identity_key, "kind": "automatic_introduction_identity",
                                   "member_id": member_id, "slack_id": uid, "reserved_at": now()})
                row.update(status="queued", reserved_at=now(), slack_id=uid,
                           trigger="first_checkout" if first else "renewal")
                _queue_message(s, row, "question")
                _maintenance(s, row, 900)
            _save(s, row)
            return True
        if ledger.store.atomic(commit):
            return
    raise RuntimeError("Introduction observation changed concurrently; retry with fresh source reads")


def _queue_message(store, row, slot):
    messages = row.setdefault("messages", {})
    messages.setdefault(slot, {"state": "ready", "client_msg_id": str(uuid5(NAMESPACE_URL, row["_id"] + ":" + slot))})
    enqueue(store, "ledger_outbox", row["_id"] + ":" + slot, "intro_send", {"member_id": row["member_id"], "slot": slot})


def _expired(row):
    return bool(row.get("deadline") and now() >= _date(row["deadline"]))


def _allowed_slot(row, slot):
    return (slot == "question" and row["status"] == "queued"
            or slot == "clarification" and row["status"] == "question"
            or slot == "explanation" and row["status"] in ("explaining", "consent"))


def _blocks(row, slot):
    affirmative = ("Explore The Ledger and opt in", "intro_open") if slot == "explanation" else ("Yes, tell me more", "intro_yes")
    return [section(TEXT[slot]), {"type": "actions", "elements": [
        button(*affirmative, row["_id"]), button("No thanks", "intro_no", row["_id"])]}]


def _available(store, row, delivered_at=None):
    if row["status"] in ("explaining", "consent") and not row.get("explanation_available_at"):
        stamp = delivered_at or now()
        row.update(status="consent", explanation_available_at=stamp, deadline=stamp + WINDOW)
        _save(store, row)
        _maintenance(store, row, WINDOW.total_seconds())


def receipt(ledger, member_id, slot, channel, ts):
    try:
        delivered = datetime.fromtimestamp(float(ts), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return
    def write(s):
        row = record(s, member_id)
        message = (row or {}).get("messages", {}).get(slot)
        if not message or message.get("channel") != channel or not isinstance(ts, str):
            return
        if message.get("ts") and message["ts"] != ts:
            # Never erase or adopt an unrelated message.
            return
        message.update(state="sent", ts=ts, delivered_at=message.get("delivered_at", delivered))
        if slot == "question" and row["status"] == "queued":
            row.update(status="question", deadline=delivered + WINDOW)
            _maintenance(s, row, WINDOW.total_seconds())
        if slot == "explanation":
            _available(s, row, delivered)
        _save(s, row)
        _maintenance(s, row)
    ledger.store.atomic(write)


def send(worker, job):
    ledger, member_id, slot = worker.ledger, job["payload"]["member_id"], job["payload"]["slot"]
    row = record(ledger.store, member_id)
    message = (row or {}).get("messages", {}).get(slot)
    if not message or message["state"] != "ready" or not _allowed_slot(row, slot):
        return
    if not enabled() or not eligible(ledger, member_id, row["slack_id"]):
        ledger.store.atomic(lambda s: _close(s, record(s, member_id), "suppressed"))
        return
    if not _running(ledger):
        from .review_notifications import ReviewDeliveryBusy
        raise ReviewDeliveryBusy()
    if worker.valid_identity(member_id) != row["slack_id"]:
        ledger.store.atomic(lambda s: _close(s, record(s, member_id), "suppressed"))
        return
    dm = worker.slack.conversations_open(users=row["slack_id"])["channel"]["id"]
    def reserve(s):
        current = record(s, member_id)
        item = current["messages"].get(slot)
        live = s.get("ledger_outbox", job["_id"])
        if not live or live.get("lease") != job.get("lease") or live["status"] != "working":
            return
        if not item or item["state"] != "ready" or not _allowed_slot(current, slot):
            return
        if _expired(current):
            _close(s, current, "expired")
            return
        if not _running(Ledger(s, ledger.sources)) or not eligible(Ledger(s, ledger.sources), member_id, current["slack_id"]):
            _close(s, current, "suppressed")
            return
        item.update(state="posting", channel=dm, attempted_at=now())
        _save(s, current)
        _maintenance(s, current, 900)
        return current
    row = ledger.store.atomic(reserve)
    if not row:
        return
    # The posting marker is permanent until there is evidence of rejection or a receipt.
    # A crash/timeout after this point must never result in an automatic second post.
    try:
        current = record(ledger.store, member_id)
        if current["status"] in TERMINAL:
            def aborted(s):
                latest = record(s, member_id)
                if not latest["messages"][slot].get("ts"):
                    latest["messages"][slot]["state"] = "aborted"
                    _save(s, latest)
            ledger.store.atomic(aborted)
            return
        response = worker.slack.chat_postMessage(channel=dm, text=TEXT[slot], blocks=_blocks(row, slot),
            client_msg_id=row["messages"][slot]["client_msg_id"], unfurl_links=False, unfurl_media=False,
            metadata={"event_type": "ledger_introduction", "event_payload": {"member_id": member_id, "slot": slot}})
        if not isinstance(response.get("ts"), str):
            raise RuntimeError("Introduction delivery has no confirmed timestamp")
        receipt(ledger, member_id, slot, dm, response["ts"])
    except Exception as exc:
        # Only an explicit Slack rejection proves that no message was accepted.
        rejected = isinstance(exc, SlackApiError) and exc.response.get("error") in {
            "ratelimited", "invalid_auth", "not_authed", "token_revoked", "account_inactive", "missing_scope",
            "invalid_blocks", "invalid_arguments", "channel_not_found", "not_in_channel", "no_permission"}
        def failed(s):
            latest = record(s, member_id)
            item = latest["messages"][slot]
            if not item.get("ts"):
                item["state"] = "ready" if rejected else "unknown"
                _save(s, latest)
                _maintenance(s, latest, 900)
        ledger.store.atomic(failed)
        if rejected:
            raise
        log.warning("Introduction delivery uncertain member=%s slot=%s", member_id, slot)


def classify(text):
    text = re.sub(r"\s+", " ", text.strip().casefold().replace("’", "'")).strip(".!? ")
    if re.fullmatch(r"(?:no|nope|nah)(?:,? (?:thanks|thank you))?|not interested|not really|"
                    r"(?:i )?(?:do not|don't) (?:like|enjoy|play)(?: (?:rpgs?|role[- ]playing games))?|"
                    r"(?:opt[- ]out|leave|stop)(?: please)?", text):
        return "no"
    if re.fullmatch(r"(?:yes|yep|yeah|sure|absolutely|definitely)(?:,? (?:please|tell me more))?|"
                    r"i do|(?:i )?(?:like|love|enjoy)(?: (?:them|rpgs?|role[- ]playing games))", text):
        return "yes"
    if re.fullmatch(r"maybe|not sure|(?:i am|i'm) not sure|i don't know|sort of|sometimes|depends|"
                    r"what do you mean|(?:please )?(?:explain|tell me more)|"
                    r"what (?:is|are) (?:that|those|role[- ]playing games|rpgs)|"
                    r"what(?:'s| is) (?:an? )?(?:rpg|role[- ]playing game)", text):
        return "unclear"
    if re.match(r"(?:yes|no|yep|yeah|sure|maybe)\b", text):
        return "unclear"
    return None


def respond(ledger, member_id, slack_id, introduction, answer):
    def change(s):
        row = record(s, member_id)
        if not row or row["_id"] != introduction or row.get("slack_id") != slack_id or row["status"] in TERMINAL:
            return None
        if _expired(row):
            _close(s, row, "expired")
            return None
        if row["status"] not in ("question", "explaining", "consent"):
            return None
        if answer == "no":
            _close(s, row, "declined")
            return row
        current = Ledger(s, ledger.sources)
        if not _running(current) or not eligible(current, member_id, slack_id):
            return None
        if answer == "yes":
            if row["status"] == "question":
                row["status"] = "explaining"
                _queue_message(s, row, "explanation")
        elif answer == "unclear" and row["status"] == "question" and not row.get("clarified"):
            row["clarified"] = True
            _queue_message(s, row, "clarification")
        _save(s, row)
        return row
    return ledger.store.atomic(change)


def action(ui, body, client, actor):
    item = body["actions"][0]
    uid = body["user"]["id"]
    row = record(ui.ledger.store, actor)
    channel = (body.get("channel") or {}).get("id")
    ts = (body.get("message") or {}).get("ts")
    if not row or not any(m.get("channel") == channel and m.get("ts") == ts for m in row["messages"].values()):
        return
    answer = "no" if item["action_id"] == "intro_no" else "yes"
    if answer == "yes":
        ui.confirm_human(actor, client)
    result = respond(ui.ledger, actor, uid, item.get("value"), answer)
    if not result or answer == "no":
        return
    from .views import consent
    ui.open(client, body, consent(introduction=result["_id"]))
    def available(s):
        latest = record(s, actor)
        if not _expired(latest):
            _available(s, latest)
    ui.ledger.store.atomic(available)


def validate_consent(ledger, member_id, introduction):
    row = record(ledger.store, member_id)
    participant = ledger.participant(member_id)
    if (row and row["_id"] == introduction and row["status"] == "joined"
            and participant and participant.get("opted_in")
            and ledger.sources.slack_id(member_id) == row.get("slack_id")):
        return
    if (not row or row["_id"] != introduction or row["status"] not in ("explaining", "consent")
            or _expired(row) or not _running(ledger) or not eligible(ledger, member_id, row.get("slack_id"))):
        raise Denied("This introduction is no longer available. Use /ledger join or a member invitation.")


def joined(ledger, member_id):
    """Called inside every join transaction, including joins from other invitation paths."""
    row = record(ledger.store, member_id)
    if row is None and not enabled():
        return
    row = row or _new(member_id)
    if row["status"] not in TERMINAL:
        row.update(status="joined", closed_at=now())
    row["joined_at"] = now()
    _save(ledger.store, row)
    if row.get("reserved_at"):
        _maintenance(ledger.store, row)


def recover(worker, row):
    for slot, message in row.get("messages", {}).items():
        if message["state"] not in ("posting", "unknown") or not message.get("channel"):
            continue
        attempted = _date(message["attempted_at"])
        response = worker.slack.conversations_history(channel=message["channel"],
            oldest=str(attempted.timestamp() - 1), latest=str((attempted + timedelta(minutes=5)).timestamp()),
            inclusive=True, limit=100, cursor=message.get("recovery_cursor", ""))
        for candidate in response.get("messages", []):
            if candidate.get("user") == worker.bot_id and candidate.get("client_msg_id") == message["client_msg_id"]:
                receipt(worker.ledger, row["member_id"], slot, message["channel"], candidate.get("ts"))
                break
        else:
            def cursor(s):
                current = record(s, row["member_id"])
                item = current["messages"][slot]
                if not item.get("ts"):
                    item.update(state="unknown", recovery_cursor=(response.get("response_metadata") or {}).get("next_cursor", ""))
                    _save(s, current)
            worker.store.atomic(cursor)


def _cleanup(worker, member_id):
    row = record(worker.store, member_id)
    if not row or row["status"] not in TERMINAL:
        return
    for slot, message in row.get("messages", {}).items():
        if not message.get("ts") or message.get("cleaned_at"):
            continue
        try:
            if row["status"] == "joined":
                worker.slack.chat_update(channel=message["channel"], ts=message["ts"],
                                         text=TEXT[slot], blocks=[section(TEXT[slot])])
            else:
                worker.slack.chat_delete(channel=message["channel"], ts=message["ts"])
        except SlackApiError as exc:
            if exc.response.get("error") != "message_not_found":
                raise
        def cleaned(s):
            latest = record(s, member_id)
            latest["messages"][slot]["cleaned_at"] = now()
            _save(s, latest)
        worker.store.atomic(cleaned)


def maintain(worker, member_id):
    """Cleanup is independent of source reads, maintenance, inference, and enablement."""
    try:
        def expire(s):
            row = record(s, member_id)
            if row and row["status"] not in TERMINAL and _expired(row):
                _close(s, row, "expired")
        worker.store.atomic(expire)
        _cleanup(worker, member_id)
        row = record(worker.store, member_id)
        if not row:
            return
        recover(worker, row)
        worker.store.atomic(expire)
        row = record(worker.store, member_id)
        if row["status"] not in TERMINAL:
            if (not enabled() or not eligible(worker.ledger, member_id, row["slack_id"])
                    or worker.valid_identity(member_id) != row["slack_id"]):
                worker.store.atomic(lambda s: _close(s, record(s, member_id), "suppressed"))
        _cleanup(worker, member_id)
    finally:
        def again(s):
            row = record(s, member_id)
            if row and _needs_maintenance(row):
                _maintenance(s, row, 900)
        worker.store.atomic(again)


def event(worker, event):
    """Consume only introduction messages; never pass their replies to inference."""
    if event.get("type") != "message" or event.get("subtype"):
        return False
    if not enabled() and not worker.store.get("ledger_catalog", CONTROL):
        return False
    channel = event.get("channel", "")
    if not channel.startswith("D"):
        return False
    if event.get("user") == worker.bot_id:
        metadata = event.get("metadata") or {}
        data = metadata.get("event_payload") or {}
        if metadata.get("event_type") != "ledger_introduction":
            return False
        row = record(worker.store, data.get("member_id"))
        message = (row or {}).get("messages", {}).get(data.get("slot"))
        if message and message.get("client_msg_id") == event.get("client_msg_id") and message.get("channel") == channel:
            receipt(worker.ledger, row["member_id"], data["slot"], channel, event.get("ts"))
        return True
    if event.get("bot_id") or event.get("bot_profile") or event.get("app_id"):
        return False
    rows = worker.store.select(COLLECTION, {"kind": "automatic_introduction", "slack_id": event.get("user")}, limit=1)
    if not rows:
        return False
    row = rows[0]
    member = worker.ledger.sources.identity(event.get("user"))
    if not member or sid(member["_id"]) != row["member_id"]:
        return False
    if not row or row.get("slack_id") != event.get("user"):
        return False
    messages = [m for m in row["messages"].values() if m.get("channel") == channel]
    if not messages:
        return False
    thread = event.get("thread_ts")
    in_thread = any(m.get("ts") == thread for m in messages) if thread else False
    if thread and not in_thread:
        return False
    answer = classify(event.get("text", ""))
    if row["status"] in TERMINAL:
        return in_thread
    if answer is None and not in_thread:
        return False
    if row["messages"].get("question", {}).get("state") in ("posting", "unknown"):
        recover(worker, row)
        row = record(worker.store, row["member_id"])
    question = row["messages"].get("question", {})
    try:
        if float(event.get("ts", "0")) <= float(question.get("ts", "inf")):
            return True
    except (TypeError, ValueError):
        return True
    respond(worker.ledger, row["member_id"], row["slack_id"], row["_id"], answer or "unclear")
    return True


def diagnostics(store):
    """Constant-sized operational counts; no member identities or message bodies."""
    kind = {"kind": "automatic_introduction"}
    rows = store.aggregate(COLLECTION, [{"$match": kind}, {"$group": {"_id": "$status", "count": {"$sum": 1}}}], max_time_ms=2000)
    return {"states": {r["_id"]: r["count"] for r in rows},
        "uncertain_deliveries": store.count(COLLECTION, {**kind, "$or": [
            {f"messages.{slot}.state": {"$in": ["posting", "unknown"]}} for slot in TEXT]}, max_time_ms=2000),
        "cleanup_pending": store.count(COLLECTION, {**kind, "status": {"$in": list(TERMINAL)}, "$or": [
            {f"messages.{slot}.ts": {"$exists": True}, f"messages.{slot}.cleaned_at": None} for slot in TEXT]}, max_time_ms=2000)}
