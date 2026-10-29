"""Private administrator avatar inspection and consent-preserving regeneration."""
import json
from uuid import uuid4

from . import avatars, views
from .admin_access import require_command
from .domain import Denied
from .messages import button, section
from .storage import enqueue, now


def authorize(ledger, actor):
    require_command(ledger, actor)
    ledger.admin(actor)


def participant(ledger, member):
    p = ledger.participant(member)
    if not p:
        raise Denied("Choose a Ledger participant.")
    return p


def picker():
    return views.modal("admin_avatar_picker", "Participant avatar", [
        views.select_input("avatar_participant", "Choose a participant"),
        section("View the participant's avatar and saved generation details. Generation is optional.")],
        submit="View avatar")


def candidates(ledger, actor, search):
    authorize(ledger, actor)
    result, cursor = [], None
    terms = search.strip().casefold()[:150].split()
    # Page owned participants and batch source identities; never scan directories.
    while len(result) < 100:
        query = {"opted_in": True, "preferences.avatars": {"$ne": False}}
        if cursor is not None:
            query["_id"] = {"$gt": cursor}
        page = ledger.store.select("ledger_participants", query, sort=[("_id", 1)],
                                   limit=32, max_time_ms=2000)
        if not page:
            break
        members = [p["member_id"] for p in page]
        identities = ledger.sources.identities(members)
        flags = {r["_id"]: r for r in ledger.store.select("ledger_catalog", {
            "_id": {"$in": ["identity:" + m for m in members]}})}
        for member in members:
            identity, flag = identities.get(member), flags.get("identity:" + member, {})
            if (not identity or identity.get("status") in ("suspended", "revoked")
                    or flag.get("bot") or flag.get("deactivated") or ledger.is_ineligible(member)):
                continue
            title = " ".join(str(identity.get(k) or "") for k in ("firstname", "lastname")).strip()
            title = (title + " (" + identity["slack_id"] + ")").strip()
            if all(term in title.casefold() for term in terms):
                result.append(views.option(title, member))
        cursor = page[-1]["_id"]
    return {"options": result[:100]}


def detail(ledger, actor, member, message=None):
    authorize(ledger, actor)
    p = participant(ledger, member)
    if not p.get("preferences", {}).get("avatars", True):
        return views.modal("dismiss", "Participant avatar", [
            section("This participant has opted out of personalized avatars.")], submit="Done")
    if not ledger.active(member):
        return views.modal("dismiss", "Participant avatar", [
            section("Personalized avatars are unavailable while this participant is opted out of The Ledger or their account is ineligible.")], submit="Done")
    blocks = [section("Participant: <@" + ledger.sources.slack_id(member) + ">")]
    if message:
        blocks.append(section(message))
    current = avatars.visible(ledger, member)
    if current:
        blocks.append({"type": "image", "slack_file": {"id": current["avatar512"]["file_id"]},
                       "alt_text": "Participant's personalized avatar"})
        if current["avatar"].get("permalink"):
            blocks.append(section("<" + current["avatar"]["permalink"] + "|Open full-resolution avatar>"))
        artifact = ledger.store.get(avatars.COLLECTION, "job:" + current["revision"]) or current
        # Show generation data, never raw chat, prompt policy, or credentials.
        fields = ("job_id", "status", "slack_username", "rank", "created_at", "job_end_time",
                  "duration_seconds", "token_usage", "avatar", "avatar512", "model", "model_revision",
                  "seed", "settings", "reference_manifest", "attempt_metrics", "prompt")
        for name in fields:
            if artifact.get(name) is None:
                continue
            text = name + ":\n" + json.dumps(artifact[name], ensure_ascii=False, indent=2, default=str)
            # Slack sections are limited to 3,000 characters. Bound the view too.
            text = text[:8000]
            for start in range(0, len(text), 2900):
                blocks.append({"type": "section", "text": {"type": "plain_text", "text": text[start:start + 2900]}})
        facts = {k: v for k, v in (artifact.get("context") or {}).items()
                 if k in ("rank", "photo_description", "bio", "interests", "tools", "shop", "shop_id",
                          "active_quests", "completed_quests")}
        if facts:
            text = "Generation profile and progress:\n" + json.dumps(facts, ensure_ascii=False, indent=2, default=str)
            text = text[:12000]
            for start in range(0, len(text), 2900):
                blocks.append({"type": "section", "text": {"type": "plain_text", "text": text[start:start + 2900]}})
    else:
        blocks.append(section("No current personalized avatar exists for this participant."))
    blocks.append({"type": "actions", "elements": [button(
        "Force regenerate avatar" if current else "Force generate avatar", "admin_avatar_generate",
        json.dumps({"member": member, "key": str(uuid4())}))]})
    return views.modal("dismiss", "Participant avatar", blocks, submit="Done")


def generate(ledger, actor, member, key):
    authorize(ledger, actor)
    if not isinstance(key, str) or not 1 <= len(key) <= 100:
        raise ValueError("Reopen /ledger-admin avatar to generate an avatar.")
    def write(store):
        live = type(ledger)(store, ledger.sources)
        authorize(live, actor)
        p = participant(live, member)
        if not p.get("preferences", {}).get("avatars", True):
            return "This participant has opted out of personalized avatars."
        if not avatars.enabled(live, member):
            raise Denied("Avatar generation is unavailable while the participant is inactive or The Ledger is paused.")
        marker = "admin-request:" + actor + ":" + key
        if store.get(avatars.COLLECTION, marker):
            return "Avatar generation is already queued."
        # Invalidate a running candidate; keep the activated pair until replacement.
        for queued in store.select("ledger_outbox", {"kind": "avatar_generate", "payload.member_id": member,
                "status": {"$in": ["pending", "working"]}}):
            was_working = queued["status"] == "working"
            queued["status"] = "cancelled"
            store.put("ledger_outbox", queued)
            artifact = store.get(avatars.COLLECTION, "job:" + queued["_id"])
            if artifact and artifact.get("status") != "activated":
                artifact.update(status="cancelled", job_end_time=now())
                store.put(avatars.COLLECTION, artifact)
                enqueue(store, "ledger_outbox", queued["_id"] + ":failed-cleanup", "avatar_cleanup",
                    {"member_id": member, "artifact_job": queued["_id"], "failed_candidate": True,
                     "wait_for_runtime": was_working,
                     "files": [artifact[k]["file_id"] for k in ("avatar", "avatar512")
                               if artifact.get(k, {}).get("file_id")]})
        live.touch(member)
        job = avatars.request(live, member, marker, delay=0)
        store.put(avatars.COLLECTION, {"_id": marker, "kind": "admin_request", "actor": actor,
            "member_id": member, "job_id": job, "at": now()})
        return "Avatar generation queued. The participant will be notified when the new avatar is ready."
    return ledger.store.atomic(write)
