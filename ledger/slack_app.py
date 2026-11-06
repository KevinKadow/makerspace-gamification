"""Bolt listeners adapt the sibling chatbot's listener pattern to durable Ledger work."""
import hashlib
import json
import re
from collections import Counter
from datetime import timedelta
from uuid import uuid4

from pymongo import timeout
from pymongo.errors import PyMongoError
from slack_bolt import App
from slack_bolt.authorization import AuthorizeResult
from slack_bolt.response import BoltResponse

from . import views
from .community import Community
from .domain import ConsentChanged, Denied, CHALLENGES
from .messages import AUDIENCES, TYPES, button, default_template, section
from .prompt_library import EXAMPLE_FACTS, library_template
from .rules import validate_rules
from .sources import sid
from .storage import enqueue, now

MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]+)?>|\b(U[A-Z0-9]+|W[A-Z0-9]+)\b")


def mentions(text):
    return [a or b for a, b in MENTION.findall(text)]


class SlackUI:
    def __init__(self, ledger, composer):
        self.ledger, self.composer = ledger, composer

    def actor(self, body):
        user = body.get("user_id") or body.get("user")
        slack_id = user.get("id") if isinstance(user, dict) else user
        member = self.ledger.sources.identity(slack_id)
        if not member:
            raise Denied("Your Slack identity must be linked to a makerspace member before using The Ledger.")
        return sid(member["_id"])

    def resolve(self, text):
        ids = mentions(text)
        if len(ids) != 1:
            raise ValueError("Specify exactly one member using @mention.")
        member = self.ledger.sources.identity(ids[0])
        if not member:
            raise ValueError("That Slack user has no active makerspace identity link.")
        return sid(member["_id"])

    def confirm_human(self, member_id, client):
        user = client.users_info(user=self.ledger.sources.slack_id(member_id))["user"]
        self.ledger.store.atomic(lambda s: s.put("ledger_catalog", {"_id": f"identity:{member_id}", "deactivated": bool(user.get("deleted")), "bot": bool(user.get("is_bot")), "at": now()}))
        if user.get("deleted") or user.get("is_bot") or user.get("id") == "USLACKBOT":
            raise Denied("Choose an active human Slack member.")

    def open(self, client, body, view):
        client.views_open(trigger_id=body["trigger_id"], view=view)

    def queue(self, member_id, command, key):
        self.ledger.store.atomic(lambda s: enqueue(s, "ledger_inbox", key, "command", {"member_id": member_id, "command": command}))

    def join_view(self, actor, sponsor=None):
        participant = self.ledger.participant(actor)
        if participant and participant["opted_in"]:
            return views.participation(self.ledger, actor)
        return views.consent(sponsor)

    def command(self, body, client):
        actor = self.actor(body)
        command, text = body["command"], body.get("text", "").strip()
        if command == "/ledger" and text in ("join", "opt-in"):
            return self.open(client, body, self.join_view(actor))
        if command == "/ledger" and text in ("leave", "opt-out"):
            return self.open(client, body, views.modal("leave_confirm", "Opt out", [section("Leave all game channels and stop game announcements. Your skills and XP remain, and eligible makerspace activity continues accruing silently. Peer kudos remains available without kudos XP. Observation is separate; use /ledger preferences to disable it.")], submit="Opt out"))
        if command == "/ledger" and text == "preferences":
            return self.open(client, body, views.preferences(self.ledger, actor))
        if command == "/ledger-admin":
            from .admin_access import require_command
            require_command(self.ledger, actor)
            operation = text.split()[0] if text else ("ranks" if self.ledger.sources.role(actor) in ("admin", "board_member") else "help")
            if operation in ("help", "review", "approve", "reject", "verify-quest", "complete-quest", "publish-quest", "reject-quest"):
                pass  # Per-evidence application authorization admits delegates.
            elif operation in ("delegates", "disable-quest"):
                self.ledger.staff(actor)
            else:
                self.ledger.admin(actor)
        elif command == "/kudos":
            self.ledger.require_member(actor)
        else:
            self.ledger.require(actor)
        if command == "/ledger" and text.split()[:1] == ["invite"] and len(mentions(text)) != 1:
            raise ValueError("Use /ledger invite @member [chat|rank:<slot>] to invite an opted-in member to a game channel.")
        if command == "/kudos":
            if text:
                recipient = self.resolve(text)
                if actor == recipient:
                    raise ValueError("You cannot give yourself kudos.")
                self.confirm_human(recipient, client)
                view = views.kudos_form(self.ledger, recipient, str(uuid4()))
            else:
                view = views.kudos_recipient()
            return self.open(client, body, view)
        if command == "/ledger-admin":
            return self.admin_command(actor, text, body, client)
        if command == "/ledger" and text in ("stats", "progress", "preferences", "achievements"):
            fn = {"stats": views.character_sheet, "progress": views.progress_view, "preferences": views.preferences, "achievements": views.achievements}[text]
            return self.open(client, body, fn(self.ledger, actor))
        if command == "/ledger-quests" and text in ("", "list"):
            return self.open(client, body, views.quest_browser(self.ledger, actor))
        if command == "/ledger-quests" and (text == "create" or text.startswith("edit ")):
            draft = None
            if text.startswith("edit "):
                key = text.split(maxsplit=1)[1]
                old = self.ledger.store.get("ledger_quests", key)
                if not old or old.get("creator") != actor or old.get("kind") != "member_quest":
                    raise Denied("Only the creator may revise this quest.")
                draft = {**old, "revision_of": key}
            return self.open(client, body, views.quest_author(self.ledger, actor, draft))
        if command == "/ledger-quests" and text.startswith("submit "):
            return self.open(client, body, views.modal("submission", "Submit learning evidence", [
                views.text_input("description", "What did you build, learn, and change?", multiline=True),
                views.text_input("learners", "Learner @mentions (mentoring only)", optional=True),
                views.text_input("mentor", "Developing mentor @mention", optional=True),
                views.text_input("handoff", "Usable handoff (required for stewardship)", optional=True, multiline=True)], {"catalog": text.split(maxsplit=1)[1], "key": str(uuid4())}, "Submit"))
        if command == "/ledger-mentor" and text == "log":
            return self.open(client, body, views.modal("submission", "Record mentoring", [
                views.text_input("description", "What skill did you help someone develop?", multiline=True),
                views.text_input("learners", "Learner @mentions"), views.select_input("shop", "Shop (optional)", optional=True)], {"catalog": "mentoring-session", "key": str(uuid4())}, "Submit"))
        if command == "/ledger-project" and (text == "new" or text.startswith("update ")):
            pid = text.split(maxsplit=1)[1] if text.startswith("update ") else None
            old = self.ledger.store.get("ledger_projects", pid) if pid else {}
            if pid and (not old or old["owner"] != actor):
                raise Denied("Only the project owner may update it.")
            return self.open(client, body, views.modal("project", "Project showcase", [
                views.text_input("title", "Project title", (old or {}).get("title", ""), max_length=100),
                views.text_input("description", "Progress, feedback wanted, and lessons", multiline=True),
                views.text_input("collaborators", "Collaborator @mentions", optional=True)], {"project": pid}, "Share"))
        key = "command:" + hashlib.sha256((body.get("trigger_id", "") + actor + command + text).encode()).hexdigest()
        self.queue(actor, command + " " + text, key)

    def admin_command(self, actor, text, body, client):
        from .admin_access import require_command, help_text
        require_command(self.ledger, actor)
        if text == "invite":
            self.ledger.admin(actor)
            return self.open(client, body, views.admin_invitation())
        if text == "help" or (text == "" and self.ledger.sources.role(actor) not in ("admin", "board_member")):
            return self.open(client, body, views.modal("dismiss", "Administration", [section(help_text(self.ledger, actor))], submit="Done"))
        if text == "ticket-quests":
            from .ticket_quests import config
            current = config(self.ledger)
            return self.open(client, body, views.modal("ticket_quest_config", "Broken ticket quests", [
                section("Configure optional quests for new damaged/broken tickets. Channel IDs are required; credentials stay in deployment secrets."),
                views.checkbox("enabled", "Enable new ticket quests", current.get("enabled", False)),
                views.text_input("quest_channel_id", "Quest announcement channel ID", current.get("quest_channel_id", "")),
                views.text_input("fix_channel_id", "Fix Tickets channel ID", current.get("fix_channel_id", "")),
                views.text_input("duration_hours", "Quest duration in hours (1–168)", str(current["duration_hours"]), max_length=3),
                views.text_input("xp_with_image", "XP with JPEG image (0–500)", str(current["xp_with_image"]), max_length=3),
                views.text_input("xp_without_image", "XP without image (0–500)", str(current["xp_without_image"]), max_length=3)], submit="Save settings"))
        if text == "delegates":
            return self.open(client, body, views.delegates(self.ledger, actor))
        if text.startswith("publish-quest ") or text.startswith("reject-quest "):
            key = text.split(maxsplit=1)[1]
            quest = self.ledger.store.get("ledger_quests", key)
            from .quest_policy import REVIEWED_KINDS, generated
            if not quest or quest.get("kind") not in REVIEWED_KINDS or quest["status"] != "pending_review":
                raise ValueError("Choose a pending reviewed quest revision.")
            from .authority import Authority
            Authority(self.ledger).authorize(actor, quest["creator"], "quest_publish", quest["shop_ids"], quest["logical_id"])
            if generated(quest):
                return self.open(client, body, views.ledger_quest_review(quest, text.startswith("publish-")))
            return self.open(client, body, views.modal("member_quest_review", "Review quest", [
                section(quest["title"]), section(quest["description"]), section(quest["criteria"]),
                views.text_input("reward", "Whole-number reward (0–500 XP)", "100"),
                views.select_input("classification", "Milestone classification", [views.option(c.replace("_", " ").title(), c) for c in sorted(CHALLENGES)]),
                views.text_input("catalog", "Existing catalog ID (specialized milestones)", optional=True), views.text_input("reason", "Review reason", optional=True)],
                {"quest": key, "approve": text.startswith("publish-")}, "Record review"))
        if text.startswith("complete-quest "):
            from .authority import Authority
            from .ledger_quests import LedgerQuests
            from .quest_policy import LEDGER_AUTHOR, cooperative
            quest = self.ledger.store.get("ledger_quests", text.split(maxsplit=1)[1])
            if not quest or not cooperative(quest):
                raise ValueError("Choose a cooperative Ledger quest.")
            project = LedgerQuests(self.ledger).project(quest) or {}
            Authority(self.ledger).authorize(actor, LEDGER_AUTHOR, "quest_complete", quest["shop_ids"],
                quest["logical_id"], excluded=project.get("contributions", {}))
            return self.open(client, body, views.modal("ledger_group_complete", "Complete shared quest", [
                section(quest["title"]), section(quest["criteria"]),
                views.text_input("description", "Verified shared outcome evidence", multiline=True)], {"quest": quest["_id"]}, "Record completion"))
        if text in ("", "ranks"):
            version = self.ledger.store.get("ledger_rulesets", "head")["version"]
            return self.open(client, body, views.ranks_form(self.ledger.store.get("ledger_rulesets", version)))
        if text.startswith("template-library "):
            tokens = text.split()
            if len(tokens) != 3:
                raise ValueError("Use /ledger-admin template-library <message type> <audience>.")
            template = library_template(tokens[1], tokens[2])
            previews = self.composer.preview(template, EXAMPLE_FACTS)
            draft_id = self.draft(actor, template)
            return self.open(client, body, views.modal("template_publish", "Preview prompt library", [
                section("*" + p["id"] + "*\nSystem: " + p["system"][:1000] + "\nUser: " + p["user"][:1600]) for p in previews] + [
                section("*Canned fallback*\n" + template["fallback"])], {"draft": draft_id}, "Publish"))
        if text.startswith("template "):
            tokens = text.split()
            if len(tokens) != 3 or tokens[1] not in TYPES or tokens[2] not in AUDIENCES:
                raise ValueError("Use /ledger-admin template <message type> <audience>.")
            template = self.composer.template(tokens[1], tokens[2])
            variations = json.dumps(template["variations"], ensure_ascii=False)
            variation_blocks = ([views.text_input("variations", "Prompt variations (JSON array)", variations, multiline=True, max_length=3000)] if len(variations) <= 3000 else
                [views.text_input(f"variation_{i}", "Paired variation: " + v["id"], json.dumps(v, ensure_ascii=False), multiline=True, max_length=3000) for i, v in enumerate(template["variations"])])
            if any(len(b["element"].get("initial_value", "")) > 3000 for b in variation_blocks):
                raise ValueError("A paired prompt exceeds the form limit. Use the versioned prompt-file workflow in docs/PROMPTS.md.")
            return self.open(client, body, views.modal("template_preview", "Message template", variation_blocks + [
                views.text_input("audience_instruction", "Audience instructions", template["audience_instruction"], multiline=True),
                views.text_input("fallback", "Canned fallback", template["fallback"], multiline=True),
                views.text_input("temperature", "Temperature", template["temperature"], max_length=10),
                views.text_input("max_tokens", "Maximum output tokens", template["max_tokens"], max_length=5)],
                {"type": tokens[1], "audience": tokens[2], "variation_count": len(template["variations"])}, "Preview"))
        if text == "catalog":
            return self.open(client, body, views.modal("catalog", "Publish catalog entry", [
                section('Shop example: {"_id":"wood-v1","kind":"shop_completion","shop_id":"..."}\nChallenge example: {"_id":"class-v1","kind":"challenge","title":"Teach a class","achievement":"boss","criteria":"Deliver and document the class","task_id":"..."}'),
                views.text_input("document", "Versioned catalog definition (JSON)", multiline=True)], submit="Publish"))
        if text == "quest":
            return self.open(client, body, views.modal("quest_create", "Create group quest", [
                views.text_input("title", "Title", max_length=100), views.text_input("criteria", "Acceptance criteria", multiline=True),
                views.text_input("roles", "Disciplines, separated by commas"), views.text_input("task", "Volunteer task ID"),
                views.text_input("shop_id", "Shop ID (optional)", optional=True)], submit="Create"))
        self.queue(actor, "/ledger-admin " + text, "admin:" + body["trigger_id"])

    def draft(self, actor, value):
        key = str(uuid4())
        self.ledger.store.atomic(lambda s: s.put("ledger_context", {"_id": key, "kind": "draft", "actor": actor,
            "value": value, "expires_at": now() + timedelta(days=1)}))
        return key

    def load_draft(self, actor, key):
        doc = self.ledger.store.get("ledger_context", key)
        if not doc or doc["actor"] != actor or doc["expires_at"] <= now():
            raise Denied("This draft expired or belongs to another member.")
        return doc["value"]

    def submission(self, body, client):
        actor = self.actor(body)
        callback = body["view"]["callback_id"]
        data, meta = views.values(body), json.loads(body["view"].get("private_metadata") or "{}")
        if callback == "dismiss":
            return {}
        if callback == "consent":
            if not data.get("agree"):
                raise ValueError("Explicitly choose to participate to continue.")
            self.confirm_human(actor, client)
            self.ledger.join(actor, meta.get("sponsor"))
            return {"response_action": "update", "view": views.participation(self.ledger, actor, "Opt-in saved")}
        if callback == "leave_confirm":
            self.ledger.leave(actor)
            return {}
        if callback == "admin_invitation":
            from .admin_access import invite, require_command
            require_command(self.ledger, actor)
            self.ledger.admin(actor)
            self.confirm_human(data["invite_recipient"], client)
            invite(self.ledger, actor, data["invite_recipient"], data.get("sender"), data.get("message"), meta["key"])
            return {}
        if callback.startswith(("ranks_", "template_")) or callback in ("catalog", "quest_create", "ticket_quest_config"):
            from .admin_access import require_command
            require_command(self.ledger, actor)
            self.ledger.admin(actor)
        elif callback in ("member_quest_review", "ledger_quest_review", "ledger_group_complete", "delegate_grant", "delegate_revoke_submit", "preferences_save"):
            if callback != "preferences_save":
                from .admin_access import require_command
                require_command(self.ledger, actor)
            # Domain methods also recheck current grants/scope transactionally.
        elif callback in ("kudos_recipient", "kudos_send"):
            self.ledger.require_member(actor)
        else:
            self.ledger.require(actor)
        if callback == "preferences_save":
            self.ledger.preferences(actor, data.get("observation", False), data.get("arrival_mentions", False))
            return {}
        if callback == "member_quest_submit":
            from .quests import Quests
            split = lambda value: [i.strip() for i in (value or "").split(",") if i.strip()]
            service = Quests(self.ledger)
            q = service.draft(actor, data["title"], data["description"], data["criteria"], int(data["target_rank"]),
                split(data.get("shops")), split(data.get("tools")), split(data.get("disciplines")), meta.get("revision_of"), key=meta["submission_key"])
            service.submit_draft(actor, q["_id"])
            return {}
        if callback == "member_quest_review":
            from .quests import Quests
            Quests(self.ledger).publish(actor, meta["quest"], int(data["reward"]), data["classification"], meta["approve"], data.get("reason", ""), data.get("catalog") or None)
            return {}
        if callback == "ticket_quest_config":
            from .ticket_quests import TicketQuests
            registered_chat = self.ledger.store.get("ledger_channels", "chat") or {}
            if data["quest_channel_id"].strip() != registered_chat.get("channel_id"):
                raise ValueError("Use the currently configured Ledge Chat channel for quest announcements.")
            fix_channel = client.conversations_info(channel=data["fix_channel_id"].strip())["channel"]
            if fix_channel.get("is_member") is not True or fix_channel.get("is_ext_shared"):
                raise ValueError("Invite The Ledger bot to a non-externally-shared Fix Tickets channel first.")
            values = {key: int(data[key]) for key in ("duration_hours", "xp_with_image", "xp_without_image")}
            values.update(enabled=data.get("enabled", False), quest_channel_id=data["quest_channel_id"].strip(),
                          fix_channel_id=data["fix_channel_id"].strip())
            TicketQuests(self.ledger).save_config(actor, values)
            return {}
        if callback == "ledger_quest_review":
            from .ledger_quests import LedgerQuests
            split = lambda value: [i.strip() for i in (value or "").split(",") if i.strip()]
            disciplines = [{"name": data.get(f"discipline_name_{i}", ""), "expectation": data.get(f"discipline_expectation_{i}", "")}
                           for i in range(4) if data.get(f"discipline_name_{i}") or data.get(f"discipline_expectation_{i}")]
            edits = {k: data[k] for k in ("title", "description", "criteria")}
            edits.update(shop_ids=split(data.get("shops")), tool_ids=split(data.get("tools")), disciplines=disciplines)
            LedgerQuests(self.ledger).review(actor, meta["quest"], int(data["reward"]), meta["approve"], data.get("reason", ""), edits)
            return {}
        if callback == "ledger_group_complete":
            from .ledger_quests import LedgerQuests
            LedgerQuests(self.ledger).finalize(actor, meta["quest"], data["description"])
            return {}
        if callback == "quest_completion_submit":
            from .quests import Quests
            learners = [self.resolve(f"<@{i}>") for i in mentions(data.get("learners") or "")]
            mentor = self.resolve(data["mentor"]) if data.get("mentor") else None
            Quests(self.ledger).submit(actor, meta["quest"], data["description"], learners, mentor, data.get("handoff"))
            return {}
        if callback == "delegate_grant":
            from .authority import Authority, CAPABILITIES
            scope = {"kind": data["scope_kind"]}
            if scope["kind"] == "shops":
                scope["shops"] = [i.strip() for i in data.get("scope_ids", "").split(",") if i.strip()]
            if scope["kind"] == "quest":
                scope["quest"] = data.get("scope_ids", "").strip()
            Authority(self.ledger).grant(actor, data["delegate"], [c for c in CAPABILITIES if data.get("cap_" + c)], scope, data["reason"])
            return {}
        if callback == "delegate_revoke_submit":
            from .authority import Authority
            Authority(self.ledger).revoke(actor, meta["grant"], data["reason"])
            return {}
        if callback == "kudos_recipient":
            recipient = data.get("recipient")
            if not recipient or recipient == actor:
                raise ValueError("Choose another member first.")
            self.confirm_human(recipient, client)
            draft = self.load_draft(actor, meta["draft"]) if meta.get("draft") else {}
            draft.pop("invitation", None)
            return {"response_action": "update", "view": views.kudos_form(self.ledger, recipient, meta["key"], draft)}
        if callback == "kudos_send":
            self.confirm_human(meta["recipient"], client)
            if not meta["participating"] and data.get("invitation") not in ("yes", "no"):
                raise ValueError("Choose whether to send kudos only or also invite this member.")
            try:
                result = self.ledger.kudos(actor, meta["recipient"], data.get("message", ""), key=meta["key"],
                    shop=data.get("shop"), tool=data.get("tool"), public=data.get("public", False),
                    invite=data.get("invitation") == "yes", expected_participation=meta["participating"], emoji=data.get("emoji"))
            except ConsentChanged:
                data.pop("invitation", None)
                return {"response_action": "update", "view": views.kudos_form(self.ledger, meta["recipient"], meta["key"], data)}
            if not result.get("result_summary"):
                # Previously accepted actions retain their original delivery flow.
                from .kudos import delivery_facts
                self.ledger.store.atomic(lambda s: enqueue(s, "ledger_outbox", f"ack:{result['_id']}", "message", {
                    "member_id": actor, "type": "delivery", "audience": "member", "exception": True, "peer_kudos": True,
                    "facts": delivery_facts(self.ledger, result, acknowledged=True)}))
        elif callback == "ranks_preview":
            ranks = []
            for i in range(1, 8):
                ranks.append({"slot": i, "name": data[f"name_{i}"].strip(), "emoji": (data.get(f"emoji_{i}") or "").strip(),
                    "floor": data.get(f"floor_{i}") or None, "requirements": json.loads(data[f"gates_{i}"]), "enabled": data[f"enabled_{i}"]})
            validate_rules({"ranks": ranks})
            draft_id = self.draft(actor, {"ranks": ranks, "base": meta["base"]})
            return {"response_action": "update", "view": views.ranks_preview(ranks, draft_id)}
        elif callback == "ranks_publish":
            draft = self.load_draft(actor, meta["draft"])
            self.ledger.publish_ranks(actor, draft["ranks"], expected=draft["base"])
        elif callback == "template_preview":
            template = {**data, **meta, "temperature": float(data["temperature"]), "max_tokens": int(data["max_tokens"])}
            template["variations"] = (json.loads(data["variations"]) if "variations" in data else
                [json.loads(data[f"variation_{i}"]) for i in range(meta["variation_count"])])
            # Preview interpolation is local; live generation is queued for an explicit test command.
            previews = self.composer.preview(template, EXAMPLE_FACTS)
            draft_id = self.draft(actor, template)
            return {"response_action": "update", "view": views.modal("template_publish", "Preview template", [
                section("*" + preview["id"] + "*\nSystem: " + preview["system"][:1000] + "\nUser: " + preview["user"][:1600]) for preview in previews] + [
                section("*Canned fallback*\n" + template["fallback"])], {"draft": draft_id}, "Publish")}
        elif callback == "template_publish":
            self.composer.publish(actor, self.load_draft(actor, meta["draft"]), self.ledger.admin)
        elif callback == "catalog":
            self.ledger.publish_catalog(actor, json.loads(data["document"]))
        elif callback == "quest_create":
            Community(self.ledger).create_quest(actor, data["title"], data["criteria"], [x.strip() for x in data["roles"].split(",") if x.strip()], data["task"], data.get("shop_id") or None)
        elif callback == "submission":
            learners = [self.resolve(f"<@{i}>") for i in mentions(data.get("learners") or "")]
            mentor = self.resolve(data["mentor"]) if data.get("mentor") else None
            self.ledger.submit(actor, meta["catalog"], data["description"], learners, shop=data.get("shop"), mentor=mentor, handoff=data.get("handoff"), key=meta.get("key"))
        elif callback == "project":
            collaborators = [self.resolve(f"<@{i}>") for i in mentions(data.get("collaborators") or "")]
            Community(self.ledger).project(actor, data["title"], data["description"], collaborators, meta.get("project"))
        else:
            raise ValueError("Unknown form.")
        return {}

    def action(self, body, client):
        actor = self.actor(body)
        action = body["actions"][0]
        name, value = action["action_id"], action.get("value", "")
        if name == "join":
            return self.open(client, body, self.join_view(actor, value or None))
        if name == "leave":
            return self.open(client, body, views.modal("leave_confirm", "Opt out", [section("Your progress is retained and continues accruing silently. All game channel access will be removed. Observation is separate; use /ledger preferences to disable it.")], submit="Opt out"))
        if name == "ack_mentoring":
            return self.ledger.acknowledge(actor, value)
        if name == "delegate_revoke":
            return self.open(client, body, views.modal("delegate_revoke_submit", "Revoke review grant", [views.text_input("reason", "Required reason", multiline=True)], {"grant": value}, "Revoke"))
        if name == "preferences":
            return self.open(client, body, views.preferences(self.ledger, actor))
        if name == "review_ledger_quest":
            return self.admin_command(actor, "publish-quest " + value, body, client)
        if name in ("shop", "kudos_change") and body.get("view", {}).get("callback_id") == "kudos_send":
            self.ledger.require_member(actor)
        else:
            self.ledger.require(actor)
        if name == "guidance_next_step":
            participant = self.ledger.require(actor)
            if value:
                owner = self.ledger.store.get("ledger_evidence", value)
                if (not owner or owner.get("kind") != "notification_summary" or owner.get("member_id") != actor or
                        owner.get("authorization") != "game" or
                        owner.get("consent_generation") != participant.get("consent_generation", 0)):
                    raise Denied("Choose your own current game result for guidance.")
            key = "guidance:" + body.get("trigger_id", action.get("action_ts", ""))
            self.ledger.store.atomic(lambda s: enqueue(s, "ledger_outbox", key, "guidance", {
                "member_id": actor, "summary_id": value or None,
                "slack_id": body["user"]["id"],
                "consent_generation": participant.get("consent_generation", 0)}))
            return
        if name in ("stats", "progress", "achievements", "preferences", "browse_quests", "quest_author"):
            fn = {"stats": views.character_sheet, "progress": views.progress_view, "achievements": views.achievements,
                  "preferences": views.preferences, "browse_quests": views.quest_browser, "quest_author": views.quest_author}[name]
            return self.open(client, body, fn(self.ledger, actor))
        if name == "skill_tree":
            return self.queue(actor, "/ledger-skills", "skills:" + body["trigger_id"])
        if name == "quest_selection":
            selected = action.get("selected_option", {}).get("value")
            view = views.quest_browser(self.ledger, actor, selected)
            return client.views_update(view_id=body["view"]["id"], hash=body["view"]["hash"], view=view)
        if name == "quest_accept":
            from .quests import Quests
            Quests(self.ledger).accept(actor, value)
            if body.get("view"):
                return client.views_update(view_id=body["view"]["id"], hash=body["view"]["hash"], view=views.quest_browser(self.ledger, actor, "q:" + value))
            return
        if name == "quest_complete_form":
            from .quests import Quests
            service = Quests(self.ledger)
            q = self.ledger.store.get("ledger_quests", value)
            if not q:
                raise ValueError("Quest not found.")
            service.eligible(actor, q, "submit")
            return self.open(client, body, views.modal("quest_completion_submit", "Submit quest evidence", [
                views.text_input("description", "Observable completion evidence", multiline=True),
                views.text_input("learners", "Learner @mentions (mentoring)", optional=True),
                views.text_input("mentor", "Developing mentor @mention", optional=True),
                views.text_input("handoff", "Usable stewardship handoff", optional=True, multiline=True)], {"quest": value}, "Submit"))
        if name == "quest_draft_help":
            from .quests import Quests
            Quests(self.ledger).targets(actor)
            data = views.values(body)
            meta = json.loads(body["view"].get("private_metadata") or "{}")
            self.ledger.store.atomic(lambda s: enqueue(s, "ledger_outbox", "draft-help:" + body["trigger_id"], "quest_draft", {
                "member_id": actor, "draft": {k: data.get(k, "") for k in ("title", "description", "criteria")},
                "original": {**data, "revision_of": meta.get("revision_of"), "submission_key": meta["submission_key"]},
                "view_id": body["view"]["id"], "view_hash": body["view"]["hash"]}))
            return
        if name == "reinvite":
            p = self.ledger.participant(actor)
            self.ledger.invite(actor, actor, "chat")
            if p["rank"]:
                self.ledger.invite(actor, actor, f"rank:{p['rank']}")
        elif name == "buddy_accept":
            Community(self.ledger).buddy(actor, value, "accept")
        elif name in ("shop", "kudos_change"):
            data, meta = views.values(body), json.loads(body["view"]["private_metadata"])
            if name == "shop":
                data["tool"] = None
                view = views.kudos_form(self.ledger, meta["recipient"], meta["key"], data)
            else:
                data.pop("invitation", None)
                draft = self.draft(actor, data)
                view = views.kudos_recipient()
                view["private_metadata"] = json.dumps({"key": meta["key"], "draft": draft})
            client.views_update(view_id=body["view"]["id"], hash=body["view"].get("hash"), view=view)

    def options(self, body):
        actor = self.actor(body)
        name, search = body["action_id"], body.get("value", "").casefold()
        if name == "invite_recipient":
            from .admin_access import invitation_candidates, require_command
            require_command(self.ledger, actor)
            self.ledger.admin(actor)
            return {"options": [views.option(title, target) for target, title in invitation_candidates(self.ledger, search)]}
        if name == "delegate":
            self.ledger.staff(actor)
            from .read_options import optimized_reads
            if optimized_reads():
                from .admin_access import delegate_candidates
                return {"options": [views.option(title, member) for member, title in delegate_candidates(self.ledger, actor, search)]}
            return {"options": [views.option(self.ledger.sources.slack_id(p["member_id"]) or "Member", p["member_id"])
                for p in self.ledger.store.select("ledger_participants", {"opted_in": True}) if p["member_id"] != actor and self.ledger.active(p["member_id"])
                and self.ledger.sources.good_standing(p["member_id"]) and search in (self.ledger.sources.slack_id(p["member_id"]) or "").casefold()][:100]}
        if name in ("recipient", "shop", "tool") and body.get("view", {}).get("callback_id") in ("kudos_recipient", "kudos_send"):
            self.ledger.require_member(actor)
        else:
            self.ledger.require(actor)
        if name == "quest_selection":
            from .quests import Quests
            return {"options": [views.option(title, key) for key, title in Quests(self.ledger).options(actor, search)]}
        options = []
        if name == "recipient":
            links = self.ledger.sources.rows("slack_users", {"invalidated_at": None})
            users = Counter(r.get("slack_id") for r in links)
            members = Counter(sid(r.get("member_id")) for r in links)
            linked = {sid(r.get("member_id")) for r in links if r.get("slack_id") and users[r["slack_id"]] == 1 and members[sid(r.get("member_id"))] == 1}
            for member in self.ledger.sources.rows("members", {"status": {"$in": ["activeMember", "pending"]}, "merged_at": None}):
                member_id = sid(member["_id"])
                title = " ".join([member.get("firstname", ""), member.get("lastname", "")]).strip()
                if member_id != actor and search in title.casefold() and member_id in linked:
                    options.append(views.option(title, member_id))
        elif name in ("shop", "tool"):
            meta = json.loads(body["view"].get("private_metadata") or "{}")
            from .sources import object_id
            query = {"disabled": {"$ne": True}}
            if name == "tool":
                query["shop_id"] = object_id(meta.get("shop"))
            for item in self.ledger.sources.rows("shops" if name == "shop" else "tools", query):
                if search in item.get("name", "").casefold():
                    options.append(views.option(item["name"], sid(item["_id"])))
        return {"options": options[:100]}


def build_app(ui, token, signing_secret, team_id, bot_id, client=None):
    def authorize(enterprise_id, team_id, user_id):
        return AuthorizeResult(enterprise_id=enterprise_id, team_id=team_id, bot_token=token, bot_user_id=bot_id)
    app = App(token=token, signing_secret=signing_secret, client=client, authorize=authorize,
              process_before_response=True, token_verification_enabled=False)

    @app.error
    def listener_error(error, logger):
        # Keep Bolt's failure status: an unpersisted request was not accepted.
        # Log only exception class and numeric Mongo code, never the payload,
        # response_url, connection string, or database error message.
        code = getattr(error, "code", None)
        logger.error("Slack listener failed error_type=%s code=%s", type(error).__name__,
                     code if isinstance(code, int) else "none")

    @app.middleware
    def workspace(body, next):
        actual = body.get("team_id") or (body.get("team") or {}).get("id")
        if actual and actual != team_id:
            return BoltResponse(status=403, body="Workspace not allowed")
        return next()

    @app.command(re.compile(r"^/(?:ledger(?:-skills|-quests|-mentor|-project|-admin)?|kudos)$"))
    def commands(ack, body, client):
        try:
            ui.command(body, client)
            ack()
        except (ValueError, KeyError, TypeError) as error:
            ack(response_type="ephemeral", text=str(error))

    @app.view(re.compile(r".*"))
    def submissions(ack, body, client):
        try:
            ack(**ui.submission(body, client))
        except (ValueError, KeyError, TypeError) as error:
            blocks = body["view"].get("blocks", [])
            field = next((b["block_id"] for b in blocks if b.get("type") == "input"), None)
            if field:
                ack(response_action="errors", errors={field: str(error)[:200]})
            else:
                ack(response_action="update", view=views.modal("dismiss", "Unable to continue", [section(str(error))], submit="Close"))

    @app.action(re.compile(r".*"))
    def actions(ack, body, client):
        try:
            ui.action(body, client)
            ack()
        except (ValueError, KeyError, TypeError) as error:
            ack()
            actor = ui.actor(body)
            ui.ledger.notify(actor, "status", {"summary": str(error)}, str(uuid4()), exception=True)

    @app.options(re.compile(r".*"))
    def options(ack, body, logger):
        try:
            with timeout(2):
                result = ui.options(body)
            ack(**result)
        except PyMongoError as error:
            logger.warning("Slack options lookup failed error_type=%s", type(error).__name__)
            ack(options=[])
        except (ValueError, KeyError):
            ack(options=[])

    @app.event(re.compile(r".*"))
    def events(ack, body):
        event = body.get("event", {})
        supported = {"app_home_opened", "app_mention", "message", "member_joined_channel", "member_left_channel", "user_change"}
        if event.get("type") in supported:
            ui.ledger.store.atomic(lambda s: enqueue(s, "ledger_inbox", "slack:" + body["event_id"], "slack_event", event))
        ack()
    return app
