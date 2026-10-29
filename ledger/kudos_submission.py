"""Durable modal acceptance; validation and receipts run outside Slack's deadline."""
from copy import deepcopy
import hashlib
import json
import re
from uuid import uuid4, uuid5, NAMESPACE_URL

from .domain import Denied
from .messages import button, escape, section
from .storage import enqueue, now
from . import views


def reserve(ui, body):
    uid = (body.get("user") or {}).get("id")
    meta = json.loads(body["view"].get("private_metadata") or "{}")
    data = views.values(body)
    if not isinstance(uid, str) or not re.fullmatch(r"[UW][A-Z0-9]+", uid):
        raise ValueError("A Slack sender is required.")
    if not isinstance(meta, dict) or not isinstance(meta.get("key"), str) or not 1 <= len(meta["key"]) <= 100:
        raise ValueError("This kudos form is invalid. Reopen /kudos.")
    if not isinstance(meta.get("recipient"), str) or not re.fullmatch(r"[a-fA-F0-9]{24}", meta["recipient"]):
        raise ValueError("Choose a valid recipient.")
    if type(meta.get("participating")) is not bool:
        raise ValueError("Reopen /kudos to review the recipient's participation.")
    message = data.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > 2000:
        raise ValueError("A kudos message of 1–2,000 characters is required.")
    if not meta["participating"] and data.get("invitation") not in ("yes", "no"):
        raise ValueError("Choose whether to send kudos only or also invite this member.")
    for field in ("shop", "tool"):
        if data.get(field) and (not isinstance(data[field], str) or not re.fullmatch(r"[a-fA-F0-9]{24}", data[field])):
            raise ValueError("Choose a valid shop/tool.")
    if data.get("emoji") is not None and (not isinstance(data["emoji"], str) or len(data["emoji"]) > 100):
        raise ValueError("Choose a valid emoji.")
    if type(data.get("public", False)) is not bool:
        raise ValueError("Choose whether to make kudos public.")
    key = "kudos-submit:" + hashlib.sha256((uid + ":" + meta["key"]).encode()).hexdigest()
    payload = {"slack_id": uid, "meta": {k: meta[k] for k in ("key", "recipient", "participating")},
        "data": {k: data.get(k) for k in ("message", "shop", "tool", "emoji", "invitation")}}
    payload["data"]["public"] = data.get("public", False)
    payload["meta"]["recipient"] = payload["meta"]["recipient"].lower()
    for field in ("shop", "tool"):
        if payload["data"].get(field):
            payload["data"][field] = payload["data"][field].lower()
    # Save only authored values and routing IDs, never the signed envelope,
    # response URLs, tokens, or arbitrary view metadata. Retries keep first input.
    ui.ledger.store.atomic(lambda s: enqueue(s, "ledger_outbox", key, "kudos_submit", payload))
    return key


def save_result(store, job, text, status):
    current = store.get("ledger_outbox", job["_id"])
    if not current or current.get("lease") != job.get("lease") or current.get("status") != "working":
        raise Denied("Kudos submission lease changed.")
    current["submission_result"] = {"text": text, "status": status, "at": now()}
    store.put("ledger_outbox", current)
    enqueue(store, "ledger_outbox", job["_id"] + ":reply", "kudos_submission_reply",
        {"request_id": job["_id"], "slack_id": job["payload"]["slack_id"]})


def process(worker, job):
    from .slack_app import SlackUI
    worker.assert_live_job(job)
    if (worker.store.get("ledger_outbox", job["_id"]) or {}).get("submission_result"):
        return
    ui = SlackUI(worker.ledger, worker.composer)
    actor = None
    try:
        actor = ui.actor({"user_id": job["payload"]["slack_id"]})
        def remember(s):
            row = s.get("ledger_outbox", job["_id"])
            if row.get("lease") != job["lease"] or row.get("status") != "working":
                raise Denied("Kudos submission lease changed.")
            row["submission_actor"] = actor
            s.put("ledger_outbox", row)
        worker.store.atomic(remember)
        worker.ledger.require_member(actor)
        response = ui.send_kudos(actor, job["payload"]["meta"], deepcopy(job["payload"]["data"]), worker.slack)
        if response.get("response_action") == "update":
            status, text = "review", "The recipient's participation changed. No kudos were sent. Review the updated warning and invitation choice."
        else:
            status, text = "accepted", "Your kudos were saved and queued for delivery. You will receive delivery confirmation."
    except ValueError as error:
        evidence = worker.store.get("ledger_evidence", "kudos:" + job["payload"]["meta"]["key"])
        if actor and evidence and evidence.get("giver") == actor:
            status, text = "accepted", "Your kudos were saved and queued for delivery. You will receive delivery confirmation."
        else:
            status, text = "rejected", str(error)
    worker.store.atomic(lambda s: save_result(s, job, text, status))


def failed(store, job):
    """Report terminal infrastructure failure without misreporting a committed send."""
    evidence = store.get("ledger_evidence", "kudos:" + job["payload"]["meta"]["key"])
    request = store.get("ledger_outbox", job["_id"])
    if request.get("submission_result"):
        return
    # A keyed commit may precede a lost response; do not claim it failed.
    uid = job["payload"]["slack_id"]
    # Caller ownership is established through current source mapping by process;
    # without that evidence the terminal notice makes no claim about another giver.
    owner = request.get("submission_actor")
    accepted = bool(evidence and owner and evidence.get("giver") == owner)
    text = ("Your kudos were saved and queued for delivery. You will receive delivery confirmation." if accepted else
        "The Ledger could not finish validating your kudos request. Use Review kudos to try again.")
    request["submission_result"] = {"status": "accepted" if accepted else "retry", "text": text, "at": now()}
    store.put("ledger_outbox", request)
    enqueue(store, "ledger_outbox", job["_id"] + ":reply", "kudos_submission_reply", {"request_id": job["_id"], "slack_id": uid})


def reply(worker, job):
    worker.assert_live_job(job)
    request = worker.store.get("ledger_outbox", job["payload"]["request_id"])
    result = request["submission_result"]
    uid = job["payload"]["slack_id"]
    user = worker.slack.users_info(user=uid)["user"]
    if user.get("deleted") or user.get("is_bot") or uid == "USLACKBOT":
        raise Denied("Slack sender is unavailable.")
    dm = worker.slack.conversations_open(users=uid)["channel"]["id"]
    blocks = [section(escape(result["text"]))]
    if result["status"] != "accepted":
        blocks.append({"type": "actions", "elements": [button("Review kudos", "kudos_retry", request["_id"])]})
    worker.assert_live_job(job)
    worker.post_message(channel=dm, text=result["text"], blocks=blocks,
        client_msg_id=str(uuid5(NAMESPACE_URL, job["_id"])), unfurl_links=False, unfurl_media=False)


def review(ui, body, client, key):
    from .kudos import require_recipient
    request = ui.ledger.store.get("ledger_outbox", key)
    uid = (body.get("user") or {}).get("id")
    if not request or request.get("kind") != "kudos_submit" or request["payload"]["slack_id"] != uid:
        raise Denied("This kudos request belongs to another member.")
    if not request.get("submission_result"):
        raise Denied("This kudos request is still being processed.")
    if (request.get("submission_result") or {}).get("status") == "accepted":
        raise Denied("These kudos were already accepted. Delivery confirmation will follow.")
    actor = ui.actor(body)
    ui.ledger.require_member(actor)
    data = deepcopy(request["payload"]["data"])
    data.pop("invitation", None)
    recipient = request["payload"]["meta"]["recipient"]
    fresh_key = str(uuid4())
    try:
        require_recipient(ui.ledger, recipient)
        ui.confirm_human(recipient, client)
    except Denied:
        view = views.kudos_recipient()
        view["private_metadata"] = json.dumps({"key": fresh_key, "draft": ui.draft(actor, data)})
    else:
        view = views.kudos_form(ui.ledger, recipient, fresh_key, data)
    return ui.open(client, body, view)
