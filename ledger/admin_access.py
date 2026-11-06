"""Application authorization, consent invitations and staff channel membership."""
from collections import Counter
import re
from uuid import NAMESPACE_URL, uuid4, uuid5

from slack_sdk.errors import SlackApiError

from .domain import Denied, Ledger
from .messages import button, escape, section
from .review_notifications import channel_id
from .sources import sid
from .storage import enqueue


def command_eligible(ledger, actor):
    if not ledger.active(actor):
        return False
    from .authority import Authority
    authority = Authority(ledger)
    if authority.staff_scope(actor):
        return True
    return any(authority.grant_valid(g) for g in ledger.store.select(
        "ledger_relationships", {"kind": "delegation", "delegate": actor, "status": "active"}))


def require_command(ledger, actor):
    if not command_eligible(ledger, actor):
        raise Denied("This action is unavailable for your current account.")


def help_text(ledger, actor):
    if not command_eligible(ledger, actor):
        return ""
    review = "review, approve <id>, reject <id> <reason>, publish-quest <id>, reject-quest <id>, verify-quest <quest> @member, complete-quest <quest>"
    if ledger.sources.role(actor) in ("admin", "board_member"):
        return "Use /ledger-admin invite, ranks, history, rollback <version>, template <type> <audience>, template-library <type> <audience>, template-history, template-rollback <id>, template-test <type> <audience>, reload-prompts, catalog, quest, ticket-quests, delegates, disable-quest <id> <reason>, coverage @member <reason>, correct-rank @member <slot> <reason>, reconcile, metrics, pause, resume, " + review + "."
    if ledger.sources.role(actor) != "resource_manager":
        from .authority import Authority
        authority = Authority(ledger)
        capabilities = {c for g in ledger.store.select("ledger_relationships", {
            "kind": "delegation", "delegate": actor, "status": "active"}) if authority.grant_valid(g) for c in g["capabilities"]}
        actions = ["review"]
        if capabilities & {"learning_review", "mentoring_review", "quest_complete"}:
            actions.extend(["approve <id>", "reject <id> <reason>"])
        if "quest_publish" in capabilities:
            actions.extend(["publish-quest <id>", "reject-quest <id>"])
        if "quest_complete" in capabilities:
            actions.extend(["verify-quest <quest> @member", "complete-quest <quest>"])
        review = ", ".join(actions)
    staff = ", delegates, disable-quest <id> <reason>" if ledger.sources.role(actor) == "resource_manager" else ""
    return "Use /ledger-admin " + review + staff + ". Reviews require current scope and independent evidence."


def invitation_eligible(ledger, target):
    return ledger.member_eligible(target) and not (ledger.participant(target) or {}).get("opted_in", False)


def delegate_candidates(ledger, actor, search):
    """Fill autocomplete after current identity checks, using bounded pages."""
    result, cursor = [], None
    while len(result) < 100:
        query = {"opted_in": True, "member_id": {"$ne": actor}}
        if cursor is not None:
            query["_id"] = {"$gt": cursor}
        page = ledger.store.select("ledger_participants", query, projection={"member_id": 1},
                                   sort=[("_id", 1)], limit=32, max_time_ms=2000)
        if not page:
            break
        members = [p["member_id"] for p in page]
        linked = ledger.sources.identities(members)
        flags = {d["_id"]: d for d in ledger.store.select("ledger_catalog", {
            "_id": {"$in": ["identity:" + m for m in members]}}, projection={"bot": 1, "deactivated": 1})}
        for member in members:
            doc, flag = linked.get(member), flags.get("identity:" + member, {})
            if (not doc or doc.get("status") not in ("activeMember", "pending") or flag.get("bot") or flag.get("deactivated")
                    or search not in doc["slack_id"].casefold()):
                continue
            result.append((member, doc["slack_id"]))
        cursor = page[-1]["_id"]
    return result[:100]


def invitation_candidates(ledger, search):
    """Read a bounded name match set and batch eligibility without per-member queries."""
    query = {"merged_at": None, "status": {"$nin": ["revoked", "suspended"]}}
    terms = search.strip()[:150].split()
    if terms:
        query["$and"] = [{"$or": [{field: {"$regex": re.escape(term), "$options": "i"}}
                                 for field in ("firstname", "lastname")]} for term in terms]
        uid = search.strip().upper()
        if re.fullmatch(r"[UW][A-Z0-9]+", uid):
            named_links = ledger.sources.bounded("slack_users", {"slack_id": uid, "invalidated_at": None}, ["member_id"], 501)
            query["$or"] = [{"$and": query.pop("$and")}, {"_id": {"$in": [r.get("member_id") for r in named_links]}}]
    members = ledger.sources.bounded("members", query, ["_id", "firstname", "lastname", "status", "merged_at"], 500)
    if not members:
        return []
    ids = [m["_id"] for m in members]
    links = ledger.sources.bounded("slack_users", {"member_id": {"$in": ids}, "invalidated_at": None},
                                   ["member_id", "slack_id"], 2 * len(ids) + 1)
    if len(links) > 2 * len(ids):
        return []  # Incomplete identity reads cannot establish eligibility.
    linked_members = Counter(sid(r.get("member_id")) for r in links)
    uids = {r["slack_id"] for r in links if isinstance(r.get("slack_id"), str) and re.fullmatch(r"[UW][A-Z0-9]+", r["slack_id"])}
    if not uids:
        return []
    all_links = ledger.sources.bounded("slack_users", {"slack_id": {"$in": sorted(uids)}, "invalidated_at": None},
                                       ["member_id", "slack_id"], 2 * len(uids) + 1)
    if len(all_links) > 2 * len(uids):
        return []
    linked_users = Counter(r.get("slack_id") for r in all_links)
    linked = {sid(r["member_id"]): r["slack_id"] for r in links
              if linked_members[sid(r.get("member_id"))] == 1 and r.get("slack_id") in uids and linked_users[r["slack_id"]] == 1}
    keys = [sid(i) for i in ids]
    opted_in = {p["_id"] for p in ledger.store.select("ledger_participants", {"_id": {"$in": keys}}) if p.get("opted_in")}
    identities = {p["_id"]: p for p in ledger.store.select("ledger_catalog", {"_id": {"$in": ["identity:" + k for k in keys]}})}
    result = []
    for member in members:
        key = sid(member["_id"])
        identity = identities.get("identity:" + key, {})
        if key not in linked or key in opted_in or identity.get("deactivated") or identity.get("bot"):
            continue
        title = " ".join(str(member.get(k) or "").strip() for k in ("firstname", "lastname")).strip() or linked[key]
        result.append((key, title))
    return result[:100]


def invite(ledger, actor, target, sender, message, key):
    def run(s):
        d = Ledger(s, ledger.sources)
        require_command(d, actor)
        d.admin(actor)
        if not invitation_eligible(d, target):
            raise Denied("Choose an eligible member who has not opted in.")
        chosen_sender, message_text = (sender or "").strip(), message or ""
        if len(chosen_sender) > 100 or len(message_text) > 2000:
            raise ValueError("Sender must be at most 100 characters and message at most 2000 characters.")
        # Only the chosen display name is available to recipient-side rendering.
        if not chosen_sender:
            member = d.sources.member(actor) or {}
            chosen_sender = " ".join(str(member.get(k) or "").strip() for k in ("firstname", "lastname")).strip() or "A makerspace administrator"
        d.touch(actor)
        d.touch(target)
        return enqueue(s, "ledger_outbox", "admin-invitation:" + key, "admin_invitation",
            {"actor": actor, "member_id": target, "sender": chosen_sender, "message": message_text})
    return ledger.store.atomic(run)


def review_eligible(ledger, member):
    return ledger.active(member) and ledger.sources.role(member) == "admin"


def sync_review_membership(ledger, member):
    """Call inside a Ledger transaction, including when paused or opting out."""
    destination = channel_id()
    key = "review-membership:" + member
    row = ledger.store.get("ledger_channels", key)
    allowed = bool(destination and review_eligible(ledger, member))
    if row and row.get("channel_id") and (not allowed or row["channel_id"] != destination):
        # Cleanup of a formerly managed membership survives config and role loss.
        job = ledger.store.get("ledger_outbox", row.get("removal_job_id", "")) or {}
        if row.get("desired") or row.get("present") or job.get("status") in ("failed", "cancelled"):
            removal = "review-remove:" + str(uuid4())
            enqueue(ledger.store, "ledger_outbox", removal, "remove", {"member_id": member,
                "slack_id": row.get("slack_id"), "channel": row["channel_id"], "review_channel": True})
            row.update(desired=False, present=False, removal_job_id=removal)
            ledger.store.put("ledger_channels", row)
    if not allowed:
        return
    generation = ledger.participant(member).get("consent_generation", 0)
    if not row or row.get("channel_id") != destination:
        row = {"_id": key, "kind": "review_membership", "member_id": member, "channel_id": destination}
    job = ledger.store.get("ledger_outbox", row.get("invitation_job_id", "")) or {}
    if row.get("desired") and row.get("consent_generation") == generation and (row.get("present") or job.get("status") in ("pending", "working")):
        return
    notice = "review-invite:" + str(uuid4())
    row.update(desired=True, consent_generation=generation, invitation_job_id=notice)
    ledger.store.put("ledger_channels", row)
    enqueue(ledger.store, "ledger_outbox", notice, "review_channel_invite", {
        "member_id": member, "channel": destination, "consent_generation": generation})


def deliver_review_invite(worker, job):
    p = job["payload"]
    member, destination = p["member_id"], p["channel"]
    def eligible():
        row = worker.store.get("ledger_channels", "review-membership:" + member) or {}
        return (destination == channel_id() and review_eligible(worker.ledger, member) and row.get("desired")
            and row.get("channel_id") == destination and row.get("consent_generation") == p["consent_generation"]
            and worker.ledger.participant(member).get("consent_generation", 0) == p["consent_generation"])
    if not eligible():
        raise Denied("Review channel invitation is no longer authorized.")
    info = worker.slack.conversations_info(channel=destination)["channel"]
    managed = {c["channel_id"] for c in worker.store.select("ledger_channels", {"kind": "channel"})}
    if (info.get("is_private") is not True or info.get("is_member") is not True or info.get("is_ext_shared")
            or info.get("is_archived") or destination in managed):
        raise ValueError("Use a private, non-external review channel containing The Ledger.")
    uid = worker.valid_identity(member)
    if not uid or not eligible():
        raise Denied("Review channel invitation is no longer authorized.")
    worker.assert_live_job(job)
    try:
        worker.slack.conversations_invite(channel=destination, users=uid)
    except SlackApiError as exc:
        if exc.response.get("error") not in ("already_in_channel", "already_in_group"):
            raise
    # Compensate withdrawal or role loss while Slack processed the invite.
    if not eligible():
        try:
            worker.slack.conversations_kick(channel=destination, user=uid)
        except SlackApiError as exc:
            if exc.response.get("error") not in ("not_in_channel", "user_not_found"):
                raise
        return
    def save(s):
        row = s.get("ledger_channels", "review-membership:" + member)
        if row and row.get("invitation_job_id") == job["_id"] and row.get("desired"):
            row["present"] = True
            row["slack_id"] = uid
            s.put("ledger_channels", row)
    worker.store.atomic(save)


def deliver_invitation(worker, job):
    p = job["payload"]
    require_command(worker.ledger, p["actor"])
    worker.ledger.admin(p["actor"])
    if not invitation_eligible(worker.ledger, p["member_id"]):
        raise Denied("Invitation is no longer eligible.")
    uid = worker.valid_identity(p["member_id"])
    if not uid:
        raise Denied("Recipient Slack identity is inactive.")
    text = escape(p["sender"]) + " invites you to join The Ledger."
    blocks = [section(text)]
    if p["message"]:
        personalized = escape(p["message"])
        text += "\n\n" + personalized
        blocks.extend(section(personalized[i:i + 2900]) for i in range(0, len(personalized), 2900))
    blocks.extend([section("Joining is your choice. Review the participation notice before opting in."),
        {"type": "actions", "elements": [button("Join The Ledger", "join", "")]}])
    dm = worker.slack.conversations_open(users=uid)["channel"]["id"]
    worker.assert_live_job(job)
    require_command(worker.ledger, p["actor"])
    worker.ledger.admin(p["actor"])
    if not invitation_eligible(worker.ledger, p["member_id"]):
        raise Denied("Invitation is no longer eligible.")
    worker.slack.chat_postMessage(channel=dm, text=text, blocks=blocks,
        client_msg_id=str(uuid5(NAMESPACE_URL, job["_id"])))
