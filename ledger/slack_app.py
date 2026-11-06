"""Bolt listeners adapt the sibling chatbot's listener pattern to durable Ledger work."""
import hashlib
import json
import re
from collections import Counter
from datetime import timedelta
from uuid import uuid4

from slack_bolt import App
from slack_bolt.authorization import AuthorizeResult
from slack_bolt.response import BoltResponse

from . import views
from .community import Community
from .domain import ConsentChanged, Denied
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

    def command(self, body, client):
        actor = self.actor(body)
        command, text = body["command"], body.get("text", "").strip()
        if command == "/ledger" and text in ("join", "opt-in"):
            return self.open(client, body, views.consent())
        if command == "/ledger" and text in ("leave", "opt-out"):
            return self.open(client, body, views.modal("leave_confirm", "Opt out", [section("Leave all game channels and stop game announcements. Your skills and XP remain, and eligible makerspace activity continues accruing silently. Peer kudos remains available without kudos XP.")], submit="Opt out"))
        if command == "/ledger-admin":
            operation = text.split()[0] if text else "ranks"
            if operation in ("review", "approve", "reject", "verify-quest"):
                self.ledger.staff(actor)
            else:
                self.ledger.admin(actor)
        else:
            self.ledger.require(actor)
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
            if len(variations) > 3000:
                raise ValueError("This prompt set exceeds Slack's 3,000-character input limit. Use the versioned prompt-file workflow documented in docs/PROMPTS.md.")
            return self.open(client, body, views.modal("template_preview", "Message template", [
                views.text_input("variations", "Prompt variations (JSON array)", variations, multiline=True, max_length=3000),
                views.text_input("audience_instruction", "Audience instructions", template["audience_instruction"], multiline=True),
                views.text_input("fallback", "Canned fallback", template["fallback"], multiline=True),
                views.text_input("temperature", "Temperature", template["temperature"], max_length=10),
                views.text_input("max_tokens", "Maximum output tokens", template["max_tokens"], max_length=5)],
                {"type": tokens[1], "audience": tokens[2]}, "Preview"))
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
            return {}
        if callback == "leave_confirm":
            self.ledger.leave(actor)
            return {}
        if callback.startswith(("ranks_", "template_")) or callback in ("catalog", "quest_create"):
            self.ledger.admin(actor)
        else:
            self.ledger.require(actor)
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
                    invite=data.get("invitation") == "yes", expected_participation=meta["participating"])
            except ConsentChanged:
                data.pop("invitation", None)
                return {"response_action": "update", "view": views.kudos_form(self.ledger, meta["recipient"], meta["key"], data)}
            self.ledger.store.atomic(lambda s: enqueue(s, "ledger_outbox", f"ack:{result['_id']}", "message", {
                "member_id": actor, "type": "delivery", "audience": "member", "facts": {"summary": "Kudos accepted. Delivery is queued; XP is awarded at most once."}}))
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
            template["variations"] = json.loads(data["variations"])
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
            return self.open(client, body, views.consent(value or None))
        if name == "leave":
            return self.open(client, body, views.modal("leave_confirm", "Opt out", [section("Your progress is retained and continues accruing silently. All game channel access will be removed.")], submit="Opt out"))
        if name == "ack_mentoring":
            return self.ledger.acknowledge(actor, value)
        self.ledger.require(actor)
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
        self.ledger.require(actor)
        name, search = body["action_id"], body.get("value", "").casefold()
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
    def options(ack, body):
        try:
            ack(**ui.options(body))
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
