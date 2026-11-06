"""Leased jobs, delivery-time authorization, and recoverable external side effects."""
import hashlib
import json
import logging
from pathlib import Path
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from slack_sdk.errors import SlackApiError
from bson import json_util

from .community import Community
from .domain import Denied, Ledger
from .messages import button, escape, section
from .prompt_library import EXAMPLE_FACTS
from .sources import FIELDS, sid
from .storage import enqueue, now
from .views import home

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, ledger, composer, slack, mqtt=None, bot_id=""):
        self.ledger, self.store = ledger, ledger.store
        self.composer, self.slack, self.mqtt, self.bot_id = composer, slack, mqtt, bot_id

    def step(self, collection, kinds=None, exclude=None):
        job = self.store.claim(collection, kinds=kinds, exclude=exclude)
        if not job:
            return False
        try:
            if collection == "ledger_inbox":
                self.inbox(job)
            else:
                self.outbox(job)
            self.finish(collection, job, "done")
        except Denied:
            self.finish(collection, job, "cancelled")
        except Exception as exc:
            # Never log event payloads, prompts, member messages, or provider responses.
            log.warning("job %s failed: %s", job["_id"], type(exc).__name__)
            retry = min(300, 2 ** min(job["attempts"], 8))
            if isinstance(exc, SlackApiError) and exc.response.status_code == 429:
                retry = max(retry, int(exc.response.headers.get("Retry-After", "60")))
            status = "pending" if job["attempts"] < 10 or job["kind"] == "remove" else "failed"
            self.finish(collection, job, status, retry, type(exc).__name__)
        return True

    def finish(self, collection, job, status, delay=0, error=None):
        def write(s):
            current = s.get(collection, job["_id"])
            if current and current.get("lease") == job["lease"] and current["status"] == "working":
                current.update(status=status, available_at=now() + timedelta(seconds=delay), last_error=error)
                s.put(collection, current)
                if collection == "ledger_outbox" and job["kind"] == "kudos" and status in ("failed", "cancelled"):
                    self.kudos_receipt(s, job, {"status": status, "at": now()})
        self.store.atomic(write)

    def inbox(self, job):
        payload = job["payload"]
        if job["kind"] == "reconcile_member":
            self.ledger.reconcile(payload["member_id"], payload.get("historical", False))
        elif job["kind"] == "reconcile_targets":
            for member_id in payload["members"]:
                if self.ledger.participant(member_id):
                    self.ledger.reconcile(member_id)
        elif job["kind"] == "reconcile":
            for p in self.store.select("ledger_participants"):
                self.valid_identity(p["member_id"])
                self.ledger.reconcile(p["member_id"])
            self.reconcile_channels()
        elif job["kind"] == "slack_event":
            self.event(payload, job["_id"])
        elif job["kind"] == "command":
            try:
                self.command(payload["member_id"], payload["command"], job["_id"])
            except (ValueError, KeyError) as exc:
                self.ledger.notify(payload["member_id"], "status", {"summary": str(exc)}, job["_id"], exception=True)

    def channel_members(self, channel):
        members, cursor = set(), None
        while True:
            response = self.slack.conversations_members(channel=channel, limit=200, cursor=cursor)
            members.update(response["members"])
            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return members

    def valid_identity(self, member_id):
        slack_id = self.ledger.sources.slack_id(member_id)
        if not slack_id:
            return None
        user = self.slack.users_info(user=slack_id)["user"]
        invalid = bool(user.get("deleted") or user.get("is_bot") or slack_id == "USLACKBOT")
        self.store.atomic(lambda s: s.put("ledger_catalog", {"_id": f"identity:{member_id}", "deactivated": invalid, "bot": bool(user.get("is_bot")), "at": now()}))
        return None if invalid else slack_id

    def reconcile_channels(self):
        for channel in self.store.select("ledger_channels", {"kind": "channel"}):
            present = self.channel_members(channel["channel_id"])
            for slack_id in present - {self.bot_id}:
                member = self.ledger.sources.identity(slack_id)
                member_id = sid(member["_id"]) if member else None
                p = self.ledger.participant(member_id) if member_id else None
                allowed = p and self.ledger.active(member_id) and (channel["_id"] == "chat" or p["rank"] >= channel.get("slot", 0))
                if not allowed:
                    self.store.atomic(lambda s, uid=slack_id, c=channel: enqueue(s, "ledger_outbox", f"unauthorized:{c['_id']}:{uid}:{uuid4()}", "remove",
                        {"slack_id": uid, "channel": c["channel_id"]}))
                if member_id:
                    key = f"membership:{member_id}:{channel['_id']}"
                    def update(s):
                        row = s.get("ledger_channels", key) or {"_id": key, "kind": "membership", "member_id": member_id, "channel_key": channel["_id"]}
                        row["present"] = True
                        s.put("ledger_channels", row)
                    self.store.atomic(update)
            for row in self.store.select("ledger_channels", {"kind": "membership", "channel_key": channel["_id"]}):
                uid = self.ledger.sources.slack_id(row["member_id"])
                if row.get("present") and uid not in present:
                    # Missing leave events must not cause unwanted automatic re-invitations.
                    row.update(present=False, voluntary_leave=True, desired=False)
                    self.store.atomic(lambda s, r=row: s.put("ledger_channels", r))

    def event(self, event, key):
        kind = event.get("type")
        if kind == "user_change":
            user = event["user"]
            member = self.ledger.sources.identity(user["id"])
            if member:
                member_id = sid(member["_id"])
                self.store.atomic(lambda s: s.put("ledger_catalog", {"_id": f"identity:{member_id}", "deactivated": bool(user.get("deleted")), "bot": bool(user.get("is_bot")), "at": now()}))
                if user.get("deleted"):
                    self.reconcile_channels()
            return
        if kind in ("member_joined_channel", "member_left_channel"):
            channels = [c for c in self.store.select("ledger_channels", {"kind": "channel"}) if c["channel_id"] == event["channel"]]
            if not channels or event["user"] == self.bot_id:
                return
            member = self.ledger.sources.identity(event["user"])
            channel = channels[0]
            if not member:
                if kind == "member_joined_channel":
                    self.store.atomic(lambda s: enqueue(s, "ledger_outbox", key + ":remove", "remove", {"slack_id": event["user"], "channel": event["channel"]}))
                return
            member_id = sid(member["_id"])
            p = self.ledger.participant(member_id)
            allowed = p and self.ledger.active(member_id) and p["rank"] >= channel.get("slot", 0)
            rowkey = f"membership:{member_id}:{channel['_id']}"
            def update(s):
                row = s.get("ledger_channels", rowkey) or {"_id": rowkey, "kind": "membership", "member_id": member_id, "channel_key": channel["_id"]}
                joined = kind == "member_joined_channel"
                row.update(present=joined, voluntary_leave=not joined, desired=bool(joined and allowed))
                s.put("ledger_channels", row)
                if joined and not allowed:
                    enqueue(s, "ledger_outbox", key + ":remove", "remove", {"member_id": member_id, "channel": event["channel"]})
            self.store.atomic(update)
            return
        member = self.ledger.sources.identity(event.get("user"))
        if kind == "app_home_opened":
            if member:
                self.slack.views_publish(user_id=event["user"], view=home(self.ledger, sid(member["_id"])))
            return
        channel = event.get("channel")
        if not channel:
            return
        if event.get("subtype") in ("message_changed", "message_deleted"):
            ts = event.get("deleted_ts") or event.get("message", {}).get("ts")
            context_key = f"message:{channel}:{ts}"
            prior = self.store.get("ledger_context", context_key)
            if prior:
                if event["subtype"] == "message_deleted":
                    self.store.atomic(lambda s: s.delete("ledger_context", context_key))
                else:
                    prior["text"] = event["message"].get("text", "")[:6000]
                    self.store.atomic(lambda s: s.put("ledger_context", prior))
            return
        if event.get("bot_id") or event.get("user") == self.bot_id or event.get("subtype"):
            return
        if not member:
            return
        member_id = sid(member["_id"])
        is_dm = event.get("channel_type") == "im" or channel.startswith("D")
        text = event.get("text", "")
        if is_dm and text.strip().lower() in ("opt out", "opt-out", "leave"):
            self.ledger.leave(member_id)
            return
        if not self.ledger.active(member_id):
            if is_dm:
                self.ledger.notify(member_id, "onboarding", {"summary": "Choose Opt in to participate in The Ledger."}, key, exception=True)
            return
        managed = {c["channel_id"] for c in self.store.select("ledger_channels", {"kind": "channel"})}
        if not is_dm and channel not in managed:
            return
        thread = event.get("thread_ts") or event.get("ts")
        addressed = is_dm or kind == "app_mention" or (self.bot_id and f"<@{self.bot_id}>" in text)
        continuing = self.store.get("ledger_context", f"thread:{channel}:{thread}")
        message_id = f"message:{channel}:{event['ts']}"
        self.store.atomic(lambda s: s.put("ledger_context", {"_id": message_id, "kind": "message", "member_id": member_id,
            "channel": channel, "thread": thread, "text": text[:6000], "at": event["ts"], "expires_at": now() + timedelta(days=30)}))
        if addressed or continuing:
            def write(s):
                s.put("ledger_context", {"_id": f"thread:{channel}:{thread}", "kind": "thread", "expires_at": now() + timedelta(days=30)})
                enqueue(s, "ledger_outbox", f"reply:{channel}:{event['ts']}", "conversation", {"member_id": member_id, "channel": channel,
                        "thread": thread, "text": text[:6000], "message_id": message_id})
            self.store.atomic(write)

    def command(self, member_id, command, key):
        from .slack_app import SlackUI
        ui = SlackUI(self.ledger, self.composer)
        words = command.split()
        cmd, args = words[0], words[1:]
        l = self.ledger
        if cmd == "/ledger-admin":
            return self.admin_command(member_id, args, key)
        l.require(member_id)
        facts = {"summary": "Use /ledger join, /ledger leave, /ledger sponsor @member, /ledger invite @member rank:1; explore /ledger-skills, /ledger-quests, /ledger-mentor, /ledger-project, and /kudos."}
        if cmd == "/ledger":
            if args and args[0] == "feedback" and len(args) > 1:
                self.store.atomic(lambda s: s.put("ledger_evidence", {"_id": key, "kind": "feedback", "member_id": member_id, "text": " ".join(args[1:])[:2000], "at": now()}))
                facts = {"summary": "Your feedback has been recorded for the pilot review. Thank you."}
            elif args and args[0] == "sponsor":
                l.sponsor(member_id, ui.resolve(" ".join(args[1:])))
                facts = {"summary": "Sponsorship invitation recorded. The recipient must explicitly opt in."}
            elif args and args[0] == "invite":
                target = ui.resolve(" ".join(args[1:]))
                channel_key = next((a for a in args if a == "chat" or a.startswith("rank:")), "chat")
                channel = self.store.get("ledger_channels", channel_key)
                if target != member_id and (not channel or l.sources.slack_id(member_id) not in self.channel_members(channel["channel_id"])):
                    raise Denied("You must be in that channel to invite another member.")
                if channel:
                    rowkey = f"membership:{member_id}:{channel_key}"
                    row = self.store.get("ledger_channels", rowkey) or {"_id": rowkey, "kind": "membership", "member_id": member_id, "channel_key": channel_key}
                    row["present"] = True
                    self.store.atomic(lambda s: s.put("ledger_channels", row))
                l.invite(member_id, target, channel_key)
                facts = {"summary": "Channel invitation queued."}
            else:
                p = l.participant(member_id)
                facts.update(rank=l.presentation(p["rank"])["name"], xp=p["xp"], metrics=p["metrics"], ruleset=p["ruleset"])
        elif cmd == "/ledger-skills":
            from io import BytesIO
            from .skills import skill_summary, render_tree
            summary = skill_summary(l, member_id, " ".join(args))
            facts = {"summary": summary["text"]}
            if summary["nodes"]:
                image = render_tree(summary)
                uid = l.sources.slack_id(member_id)
                dm = self.slack.conversations_open(users=uid)["channel"]["id"]
                self.slack.files_upload_v2(file_uploads=[{"file": image, "filename": "ledger-skill-tree.png", "title": "Your skill tree"},
                    {"file": BytesIO(summary["text"].encode()), "filename": "ledger-skill-tree.txt", "title": "Complete skill tree text equivalent"}], channel=dm)
        elif cmd == "/ledger-mentor":
            c = Community(l)
            if args and args[0] == "offer":
                rel = c.buddy(member_id, ui.resolve(" ".join(args[1:])))
                facts = {"summary": "Success Buddy offer recorded.", "relationship": rel["_id"]}
            elif args and args[0] in ("accept", "end") and len(args) == 2:
                c.buddy(member_id, args[1], args[0])
                facts = {"summary": "Buddy relationship updated."}
            else:
                facts = {"summary": "Use /ledger-mentor offer @member, /ledger-mentor log, or /ledger-mentor end <relationship id>. Formal checkout teaching is recognized automatically."}
        elif cmd == "/ledger-quests":
            c = Community(l)
            if args and args[0] == "join" and len(args) >= 3:
                c.quest(member_id, args[1], "join", role=" ".join(args[2:]))
                facts = {"summary": "You joined the quest. Coordinate roles and submit your contribution with /ledger-quests contribute <id> <description>."}
            elif args and args[0] == "contribute" and len(args) >= 3:
                c.quest(member_id, args[1], "submit", description=" ".join(args[2:]))
                facts = {"summary": "Contribution submitted for independent verification."}
            else:
                catalog = self.store.select("ledger_catalog", {"kind": "challenge", "active": True})
                quests = self.store.select("ledger_quests", {"status": "open"})
                facts = {"summary": "Choose a challenge, then /ledger-quests submit <id>. Join group quests with /ledger-quests join <id> <discipline>.",
                         "challenges": [{"id": r["_id"], "title": r.get("title"), "criteria": r.get("criteria")} for r in catalog],
                         "quests": [{"id": q["_id"], "title": q["title"], "roles": q["roles"], "criteria": q["criteria"]} for q in quests]}
        elif cmd == "/ledger-project":
            facts = {"summary": "Use /ledger-project new or /ledger-project update <id>. The project gallery is on The Ledger's Home tab.",
                     "projects": [{"id": p["_id"], "title": p["title"], "url": p.get("permalink")} for p in self.store.select("ledger_projects")[-10:]]}
        l.notify(member_id, "status", facts, key)

    def admin_command(self, actor, args, key):
        l = self.ledger
        if args and args[0] in ("review", "approve", "reject", "verify-quest"):
            l.staff(actor)
        else:
            l.admin(actor)
        summary = "Use /ledger-admin ranks, history, rollback <version>, template <type> <audience>, template-library <type> <audience>, template-history, template-rollback <id>, template-test <type> <audience>, catalog, quest, review, approve <id>, reject <id> <reason>, verify-quest <quest> @member, coverage @member <reason>, correct-rank @member <slot> <reason>, reconcile."
        if args and args[0] == "history":
            summary = "\n".join(f"{r['_id']} — {r.get('at', 'initial')}" for r in self.store.select("ledger_rulesets") if r["_id"] != "head")
        elif args and args[0] == "rollback" and len(args) == 2:
            r = l.rollback_ranks(actor, args[1])
            summary = "Published a new progression version from " + args[1] + ": " + r["_id"]
        elif args and args[0] == "template-history":
            summary = "\n".join(f"{t['_id']} {t['type']}/{t['audience']}" for t in self.store.select("ledger_message_templates") if "type" in t)
        elif args and args[0] == "template-rollback" and len(args) == 2:
            t = self.store.get("ledger_message_templates", args[1])
            if not t or "type" not in t:
                raise ValueError("Unknown template version")
            self.composer.publish(actor, t, l.admin)
            summary = "Template rollback published as a new version."
        elif args and args[0] == "template-test" and len(args) == 3:
            output = self.composer.compose(args[1], args[2], EXAMPLE_FACTS)
            summary = f"Preview ({output['outcome']}, {output['prompt_variation']}): {output['text']}"
        elif args and args[0] == "review":
            rows = []
            for r in self.store.select("ledger_evidence", {"kind": "submission", "status": "pending"}):
                try:
                    l.reviewer(actor, r["member_id"], r.get("shop_id"))
                    rows.append(r)
                except Denied:
                    continue
            summary = "\n".join(f"{r['_id']}: {r['achievement']} — {r['description']}" for r in rows) or "No pending submissions you can review."
        elif args and args[0] in ("approve", "reject") and len(args) >= 2:
            l.review(actor, args[1], args[0] == "approve", " ".join(args[2:]))
            summary = "Review recorded."
        elif args and args[0] == "verify-quest" and len(args) >= 3:
            from .slack_app import SlackUI
            target = SlackUI(l, self.composer).resolve(" ".join(args[2:]))
            Community(l).quest(actor, args[1], "verify", member=target)
            summary = "Quest contribution verified."
        elif args and args[0] in ("coverage", "correct-rank", "release-rank"):
            from .slack_app import SlackUI
            target = SlackUI(l, self.composer).resolve(" ".join(args[1:]))
            if args[0] == "coverage":
                l.coverage(actor, target, " ".join(args[2:]))
            elif args[0] == "release-rank":
                l.release_rank(actor, target, " ".join(args[2:]))
            else:
                l.correct_rank(actor, target, int(args[2]), " ".join(args[3:]))
            summary = "Independent attestation/correction recorded."
        elif args and args[0] == "reconcile":
            self.store.atomic(lambda s: enqueue(s, "ledger_inbox", "manual-reconcile:" + key, "reconcile", {}))
            summary = "Reconciliation queued."
        elif args and args[0] in ("pause", "resume"):
            self.store.atomic(lambda s: s.put("ledger_catalog", {"_id": "control", "paused": args[0] == "pause", "actor": actor, "at": now()}))
            summary = "Game processing paused; opt-out and channel cleanup remain available." if args[0] == "pause" else "Game processing resumed."
        elif args and args[0] == "metrics":
            from .metrics import snapshot
            summary = json.dumps(snapshot(self.store), default=str)
        l.notify(actor, "status", {"summary": summary}, key, exception=True)

    def persist_composition(self, job, kind, audience, facts, conversation=None):
        current = self.store.get("ledger_outbox", job["_id"])
        if current.get("composed"):
            return current["composed"]
        payload = job["payload"]
        # Channel conversations use the same shared history as announcements,
        # even though their conversational prompt uses the member audience.
        shared = audience == "shared" or (job["kind"] == "conversation" and not payload["channel"].startswith("D"))
        scope = "shared" if shared else "member:" + payload["member_id"]
        def reserve(s):
            saved = s.get("ledger_outbox", job["_id"])
            if saved.get("lease") != job["lease"] or saved["status"] != "working":
                raise Denied("Delivery was cancelled or its lease expired.")
            if not saved.get("prompt_selection"):
                saved["prompt_selection"] = self.composer.reserve(s, kind, audience, scope)
                s.put("ledger_outbox", saved)
            return saved["prompt_selection"]
        selection = self.store.atomic(reserve)
        facts = self.prompt_facts(job["payload"].get("member_id"), kind, facts)
        result = self.composer.compose(kind, audience, facts, conversation, selection=selection)
        def write(s):
            saved = s.get("ledger_outbox", job["_id"])
            if saved.get("lease") != job["lease"] or saved["status"] != "working":
                raise Denied("Delivery was cancelled or its lease expired.")
            saved["composed"] = result
            s.put("ledger_outbox", saved)
        self.store.atomic(write)
        return result

    def identity_facts(self, member_id, prefix="member"):
        member = self.ledger.sources.member(member_id) or {}
        name = " ".join(str(member.get(k) or "").strip() for k in ("firstname", "lastname")).strip()
        return {prefix + "_full_name": name or None, prefix + "_slack_id": self.ledger.sources.slack_id(member_id)}

    def prompt_facts(self, member_id, kind, facts):
        facts = {**facts, **self.identity_facts(member_id)}
        # Kudos and invitations must remain safe if participation changes after
        # composition; never include retained rank, XP, or skill projections.
        if kind not in ("kudos", "invitation") and self.ledger.active(member_id):
            participant = self.ledger.participant(member_id)
            facts["current_rank"] = self.ledger.presentation(participant["rank"])["name"]
            facts.setdefault("xp_total", participant["xp"])
            if kind in ("rank_up", "return", "checkout_earned", "conversation"):
                from .skills import highest_skill
                facts.update(highest_skill(self.ledger.sources, member_id))
        if kind == "invitation" and facts.get("sponsor"):
            facts.update(self.identity_facts(facts["sponsor"], "sponsor"))
        return facts

    def assert_live_job(self, job):
        current = self.store.get("ledger_outbox", job["_id"])
        if current["status"] != "working" or current.get("lease") != job["lease"]:
            raise Denied("Delivery was cancelled.")

    def outbox(self, job):
        p = job["payload"]
        kind = job["kind"]
        member_id = p.get("member_id")
        if kind == "remove":
            uid = p.get("slack_id") or self.ledger.sources.slack_id(member_id)
            if uid:
                try:
                    self.slack.conversations_kick(channel=p["channel"], user=uid)
                except SlackApiError as e:
                    if e.response.get("error") not in ("not_in_channel", "user_not_found"):
                        raise
            return
        if (self.store.get("ledger_catalog", "control") or {}).get("paused") and not p.get("exception"):
            raise Denied("Game delivery paused by an operator.")
        if kind == "provision_slot":
            slot = p["slot"]
            if not self.store.get("ledger_channels", f"rank:{slot}"):
                channel = self.slack.conversations_create(name=f"ledger-rank-{slot}", is_private=True)["channel"]
                self.store.atomic(lambda s: s.put("ledger_channels", {"_id": f"rank:{slot}", "kind": "channel", "channel_id": channel["id"], "slot": slot}))
            return
        if kind == "invite":
            participant = self.ledger.participant(member_id)
            channel = self.store.get("ledger_channels", p["channel_key"])
            membership = self.store.get("ledger_channels", f"membership:{member_id}:{p['channel_key']}") or {}
            if not self.ledger.active(member_id) or not participant or participant["revision"] < p["revision"] or membership.get("voluntary_leave") or not membership.get("desired"):
                raise Denied("Channel invitation is no longer authorized.")
            if participant["rank"] < channel.get("slot", 0):
                raise Denied("Rank requirement not met.")
            uid = self.valid_identity(member_id)
            if not uid:
                raise Denied("Slack identity inactive.")
            self.assert_live_job(job)
            try:
                self.slack.conversations_invite(channel=p["channel"], users=uid)
            except SlackApiError as e:
                if e.response.get("error") not in ("already_in_channel", "already_in_group"):
                    raise
            # Compensate an opt-out that happened while Slack processed the invitation.
            latest_membership = self.store.get("ledger_channels", membership["_id"]) or {}
            if not self.ledger.active(member_id) or latest_membership.get("voluntary_leave") or not latest_membership.get("desired"):
                self.slack.conversations_kick(channel=p["channel"], user=uid)
            return
        if kind == "mqtt":
            if not self.ledger.active(member_id):
                raise Denied("Silent accrual has no external advancement announcements.")
            if self.mqtt is None:
                raise RuntimeError("MQTT publisher unavailable")
            self.assert_live_job(job)
            result = self.mqtt.publish("ledger/v1/advancements", json.dumps(p, default=str), qos=1, retain=False)
            result.wait_for_publish(timeout=5)
            if not result.is_published():
                raise RuntimeError("MQTT publication not acknowledged")
            return
        if kind == "kudos":
            return self.deliver_kudos(job)
        if kind == "project":
            return self.deliver_project(job)
        uid = self.valid_identity(member_id)
        if not uid or not self.ledger.sources.permitted(member_id):
            raise Denied("Recipient cannot receive this message.")
        if not p.get("exception") and not self.ledger.active(member_id):
            raise Denied("Recipient opted out.")
        if kind == "rank_art":
            from .rules import RANKS
            slot = p["slot"]
            display = self.ledger.presentation(slot)
            # Supplied art has baked-in labels. Never show an obsolete rank name.
            if display["name"] == RANKS[slot - 1][0]:
                dm = self.slack.conversations_open(users=uid)["channel"]["id"]
                self.assert_live_job(job)
                self.slack.files_upload_v2(file=str(Path(__file__).parent / "assets" / f"rank-{slot}.png"),
                                          title=display["name"], channel=dm)
            return
        if kind == "conversation":
            request = self.store.get("ledger_context", p["message_id"])
            if not request:
                raise Denied("The original request was deleted.")
            context = [c for c in self.store.select("ledger_context", {"kind": "message", "channel": p["channel"], "thread": p["thread"]})
                       if c["_id"] != p["message_id"] and self.ledger.active(c["member_id"])]
            context.sort(key=lambda c: c["at"])
            history = [{"role": "user", "content": c["text"]} for c in context[-10:]]
            participant = self.ledger.participant(member_id)
            facts = {"request": request["text"], "rank": self.ledger.presentation(participant["rank"])["name"], "xp": participant["xp"], "metrics": participant["metrics"]}
            composed = self.persist_composition(job, "conversation", "member", facts, history)
            self.assert_live_job(job)
            latest_request = self.store.get("ledger_context", p["message_id"])
            if not self.ledger.active(member_id) or not latest_request or latest_request["text"] != request["text"]:
                raise Denied("Member opted out during generation.")
            self.slack.chat_postMessage(channel=p["channel"], thread_ts=p["thread"], text=composed["text"],
                client_msg_id=str(uuid5(NAMESPACE_URL, job["_id"])))
            return
        audience, facts = p["audience"], p.get("facts", {})
        facts = dict(facts)
        if p["type"] in ("return", "onboarding") and self.ledger.active(member_id):
            participant = self.ledger.participant(member_id)
            if participant.get("import_pending"):
                raise RuntimeError("Waiting for history import")
            facts.update(rank=self.ledger.presentation(participant["rank"])["name"], xp=participant["xp"], metrics=participant["metrics"])
        def current_labels(fact):
            if isinstance(fact, dict):
                fact = {k: current_labels(v) for k, v in fact.items()}
                if fact.get("slot") and "rank" in fact:
                    fact["rank"] = self.ledger.presentation(fact["slot"])["name"]
                for prefix in ("old", "new"):
                    if fact.get(prefix + "_slot"):
                        fact[prefix + "_rank"] = self.ledger.presentation(fact[prefix + "_slot"])["name"]
                return fact
            return [current_labels(f) for f in fact] if isinstance(fact, list) else fact
        facts = current_labels(facts)
        composed = self.persist_composition(job, p["type"], audience, facts)
        self.assert_live_job(job)
        if not p.get("exception") and not self.ledger.active(member_id):
            raise Denied("Member opted out during generation.")
        channel = self.shared_channel() if audience == "shared" else self.slack.conversations_open(users=uid)["channel"]["id"]
        visible = {k: v for k, v in facts.items() if k not in ("sponsor", "buddy", "submission")}
        canonical = "\n".join(([visible["summary"]] if visible.get("summary") else []) +
                              [f"{k.replace('_', ' ').title()}: {json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (list, dict)) else v}" for k, v in visible.items() if k != "summary"])
        if not canonical:
            canonical = "Choose whether to participate using the button below."
        blocks = [section(composed["text"]), section(escape(canonical)[:2900])]
        if audience == "shared":
            display = self.ledger.presentation(self.ledger.participant(member_id)["rank"])
            blocks.insert(0, section(f"{display['emoji']} <@{uid}> · {escape(display['name'])}"))
            canonical = f"{display['name']} <@{uid}>\n" + canonical
        if p["type"] in ("onboarding", "invitation") and not self.ledger.active(member_id):
            sponsor = facts.get("sponsor", "")
            blocks.append({"type": "actions", "elements": [button("Opt in to The Ledger", "join", sponsor)]})
        if facts.get("submission"):
            blocks.append({"type": "actions", "elements": [button("Acknowledge mentoring", "ack_mentoring", facts["submission"])]})
        if facts.get("buddy"):
            blocks.append({"type": "actions", "elements": [button("Accept Success Buddy", "buddy_accept", facts["buddy"])]})
        self.slack.chat_postMessage(channel=channel, text=composed["text"] + "\n" + canonical, blocks=blocks,
                                   client_msg_id=str(uuid5(NAMESPACE_URL, job["_id"])), unfurl_links=False, unfurl_media=False)

    def shared_channel(self):
        row = self.store.get("ledger_channels", "chat")
        if not row:
            raise RuntimeError("Ledge Chat is not registered; run bootstrap before inviting participants")
        return row["channel_id"]

    def deliver_kudos(self, job):
        p = job["payload"]
        e = self.store.get("ledger_evidence", p["evidence"])
        if e["deliveries"].get(p["audience"], {}).get("ts"):
            return
        recipient, giver = e["recipient"], e["giver"]
        if not self.ledger.sources.good_standing(recipient):
            raise Denied("Recipient no longer in good standing.")
        uid = self.valid_identity(recipient)
        giver_uid = self.ledger.sources.slack_id(giver)
        if not uid or not giver_uid:
            raise Denied("A linked human Slack identity is required.")
        public = p["audience"] == "shared"
        if public and not self.ledger.active(giver):
            raise Denied("Giver no longer participates; public delivery cancelled.")
        audience = "shared" if public else "recipient" if self.ledger.active(recipient) else "nonparticipant"
        composed = self.persist_composition(job, "kudos", audience, {"giver": giver_uid, "recipient": uid,
                    **self.identity_facts(giver, "giver"), **self.identity_facts(recipient, "recipient"),
                    "shop": e.get("shop_name"), "tool": e.get("tool_name")})
        self.assert_live_job(job)
        if not self.ledger.sources.good_standing(recipient) or (public and not self.ledger.active(giver)):
            raise Denied("Kudos delivery is no longer permitted.")
        identity = self.store.get("ledger_catalog", f"identity:{recipient}") or {}
        if identity.get("deactivated") or identity.get("bot"):
            raise Denied("Recipient identity is no longer active.")
        giver_rank = self.ledger.presentation(self.ledger.participant(giver)["rank"])
        recipient_rank = self.ledger.presentation(self.ledger.participant(recipient)["rank"]) if self.ledger.active(recipient) else None
        header = f"{giver_rank['emoji']} <@{giver_uid}> → "
        if recipient_rank:
            header += recipient_rank["emoji"] + " "
        header += f"<@{uid}>"
        accessible = f"{giver_rank['name']} <@{giver_uid}> gives kudos to " + (recipient_rank["name"] + " " if recipient_rank else "") + f"<@{uid}>"
        blocks = [section(header), section(composed["text"]), section(e["message"])]
        if e.get("shop_name"):
            blocks.append(section("Context: " + escape(e["shop_name"]) + (" / " + escape(e["tool_name"]) if e.get("tool_name") else "")))
        if not public and e.get("invite") and not self.ledger.active(recipient):
            rel = self.store.get("ledger_relationships", f"sponsor:{recipient}") or {}
            blocks.append({"type": "actions", "elements": [button("Explore The Ledger and opt in", "join", rel.get("giver", giver))]})
        channel = self.shared_channel() if public else self.slack.conversations_open(users=uid)["channel"]["id"]
        response = self.slack.chat_postMessage(channel=channel, text=accessible + "\n" + e["message"], blocks=blocks,
            client_msg_id=str(uuid5(NAMESPACE_URL, job["_id"])), unfurl_links=False, unfurl_media=False)
        def receipt(s):
            self.kudos_receipt(s, job, {"status": "delivered", "channel": channel, "ts": response["ts"], "at": now()})
        self.store.atomic(receipt)

    def kudos_receipt(self, s, job, receipt):
        e = s.get("ledger_evidence", job["payload"]["evidence"])
        audience = job["payload"]["audience"]
        if e["deliveries"].get(audience, {}).get("ts"):
            return
        e["deliveries"][audience] = receipt
        s.put("ledger_evidence", e)
        destinations = [("recipient", "DM")] + ([("shared", "Ledge Chat")] if e["public"] else [])
        summary = "; ".join(label + ": " + e["deliveries"].get(key, {}).get("status", "pending") for key, label in destinations)
        Ledger(s, self.ledger.sources).notify(e["giver"], "delivery", {"summary": summary}, f"receipt:{e['_id']}:{audience}:{receipt['status']}")

    def deliver_project(self, job):
        p = job["payload"]
        self.ledger.require(p["member_id"])
        project = self.store.get("ledger_projects", p["project_id"])
        update = project["updates"][p["update"]]
        if update.get("delivery"):
            if not project.get("permalink"):
                url = self.slack.chat_getPermalink(conversation_id=update["delivery"]["channel"], message_ts=project["thread_ts"])["permalink"]
                def restore_link(s):
                    latest = s.get("ledger_projects", project["_id"])
                    latest["permalink"] = url
                    s.put("ledger_projects", latest)
                self.store.atomic(restore_link)
            return
        composed = self.persist_composition(job, "project", "shared", {"title": project["title"]})
        self.assert_live_job(job)
        self.ledger.require(p["member_id"])
        channel = self.shared_channel()
        people = [project["owner"]] + [m for m in project["collaborators"] if m != project["owner"] and self.ledger.active(m)]
        credit = "Contributors: " + ", ".join(f"<@{self.ledger.sources.slack_id(m)}>" for m in people)
        response = self.slack.chat_postMessage(channel=channel, thread_ts=project.get("thread_ts"),
            text=project["title"] + "\n" + credit + "\n" + update["description"], blocks=[section("*" + escape(project["title"]) + "*"), section(credit), section(composed["text"]), section(update["description"])],
            client_msg_id=str(uuid5(NAMESPACE_URL, job["_id"])))
        def save_receipt(s):
            latest = s.get("ledger_projects", project["_id"])
            latest["updates"][p["update"]]["delivery"] = {"channel": channel, "ts": response["ts"]}
            latest.setdefault("thread_ts", response["ts"])
            s.put("ledger_projects", latest)
        self.store.atomic(save_receipt)
        if not project.get("thread_ts"):
            url = self.slack.chat_getPermalink(conversation_id=channel, message_ts=response["ts"])["permalink"]
            def save(s):
                latest = s.get("ledger_projects", project["_id"])
                latest.update(thread_ts=response["ts"], permalink=url)
                s.put("ledger_projects", latest)
            self.store.atomic(save)


def ingest_mqtt(store, topic, payload):
    """Legacy bridge payloads are not event envelopes. Keep only a reconciliation trigger."""
    collection, operation = topic.split("/", 1)
    if collection not in FIELDS or operation not in ("insert", "update", "replace", "delete"):
        return False
    prefix, timestamp, document = payload.decode().split(" ", 2)
    if prefix != operation or not timestamp.isdigit():
        raise ValueError("Invalid bridge payload")
    envelope = json_util.loads(document)
    doc = envelope.get("document") if isinstance(envelope, dict) else None
    key = "mqtt:" + hashlib.sha256(topic.encode() + payload).hexdigest()
    # Delivery order is only a hint. The worker rereads canonical Mongo records.
    targets = set()
    if isinstance(doc, dict) and operation != "delete":
        if collection in ("tool_checkouts", "volunteer_credits", "earned_memberships"):
            targets = {sid(doc.get("member_id")), sid(doc.get("approved_by_id"))} - {None}
    if targets:
        store.atomic(lambda s: enqueue(s, "ledger_inbox", key, "reconcile_targets", {"members": sorted(targets)}))
    else:
        store.atomic(lambda s: enqueue(s, "ledger_inbox", key, "reconcile", {}))
    return True
