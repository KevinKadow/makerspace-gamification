from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from .rules import amount, attainable_rank, default_rules, validate_rules
from .sources import object_id, sid
from .storage import enqueue, now

MAJOR = {"rank_up", "shop_complete", "boss", "stewardship"}
CHALLENGES = {"challenge", "first_build", "boss", "stewardship", "develop_mentor", "mentoring"}
RESULT_XP = CHALLENGES | {"checkout_earned", "checkout_granted", "volunteer_credit", "quest", "recruitment"}


def enqueue_home_refresh(store, member_id, trigger, slack_id=None):
    key = f"home:{member_id}:{trigger}"
    enqueue(store, "ledger_outbox", key, "home_publish", {"member_id": member_id, "slack_id": slack_id})


class Denied(ValueError):
    pass


class ConsentChanged(ValueError):
    pass


class Ledger:
    def __init__(self, store, sources):
        self.store, self.sources = store, sources

    def tx(self, method, *args, **kwargs):
        return self.store.atomic(lambda s: getattr(Ledger(s, self.sources), method)(*args, **kwargs))

    def seed(self):
        def create(s):
            if not s.get("ledger_rulesets", "head"):
                initial = default_rules()
                s.put("ledger_rulesets", initial)
                s.put("ledger_rulesets", {"_id": "head", "version": "initial"})
                s.put("ledger_catalog", {"_id": "rank_display", "ranks": initial["ranks"]})
            for name, kind in [("first-build", "first_build"), ("learning-challenge", "challenge"), ("mentoring-session", "mentoring")]:
                if not s.get("ledger_catalog", name):
                    s.put("ledger_catalog", {"_id": name, "kind": "challenge", "achievement": kind,
                        "title": {"first_build": "First Build: a personalized keychain or your own small build", "challenge": "Self-directed learning challenge", "mentoring": "Share a skill"}[kind],
                        "criteria": "Document the skill shared and obtain each learner's acknowledgment." if kind == "mentoring" else "Document a safe small build, the shop rules and tools you learned, and feedback you applied. An accessible self-directed alternative is welcome.", "active": True})
        self.store.atomic(create)

    def backfill_sponsor_invitations(self):
        from .sponsorships import backfill
        return self.store.atomic(backfill)

    def participant(self, member_id):
        return self.store.get("ledger_participants", str(member_id))

    def preference_profile(self, member_id):
        p = self.participant(member_id)
        if p:
            return p
        profile = self.store.get("ledger_relationships", f"member-preferences:{member_id}")
        if profile and profile.get("kind") == "member_preferences":
            return profile
        return {
            "_id": f"member-preferences:{member_id}", "kind": "member_preferences", "member_id": member_id,
            "revision": 0, "preferences": {"observation": True, "arrival_mentions": True}}

    def save_preference_profile(self, profile):
        collection = "ledger_relationships" if profile.get("kind") == "member_preferences" else "ledger_participants"
        self.store.put(collection, profile)

    def member_eligible(self, member_id):
        from .ineligible import is_ineligible_member
        identity = self.store.get("ledger_catalog", f"identity:{member_id}") or {}
        return bool(not is_ineligible_member(self, member_id) and self.sources.permitted(member_id)
                    and not identity.get("deactivated") and not identity.get("bot"))

    def active(self, member_id):
        from .ineligible import is_ineligible_member
        p = self.participant(member_id)
        identity = self.store.get("ledger_catalog", f"identity:{member_id}") or {}
        return bool(not is_ineligible_member(self, member_id) and p and p["opted_in"]
                    and not identity.get("deactivated") and not identity.get("bot")
                    and self.sources.permitted(member_id))

    def is_ineligible(self, member_id):
        from .ineligible import is_ineligible_member
        return is_ineligible_member(self, member_id)

    def staff(self, actor):
        if not self.sources.permitted(actor) or self.sources.role(actor) not in ("admin", "board_member", "resource_manager"):
            raise Denied("This action requires makerspace staff permissions.")

    def require(self, member_id):
        if (self.store.get("ledger_catalog", "control") or {}).get("paused"):
            raise Denied("The Ledger is paused for maintenance. Opt-out remains available.")
        if not self.active(member_id):
            raise Denied("Opt in to The Ledger with a valid linked Slack account first.")
        return self.participant(member_id)

    def require_member(self, member_id):
        """Peer kudos/chat require a live human identity, not game consent."""
        if (self.store.get("ledger_catalog", "control") or {}).get("paused"):
            raise Denied("The Ledger is paused for maintenance. Opt-out remains available.")
        identity = self.store.get("ledger_catalog", f"identity:{member_id}") or {}
        if not self.sources.permitted(member_id) or identity.get("deactivated") or identity.get("bot"):
            raise Denied("A valid linked human Slack account is required.")

    def admin(self, actor):
        if not self.sources.permitted(actor) or self.sources.role(actor) not in ("admin", "board_member"):
            raise Denied("Only admins and board members may perform this action.")

    def reviewer(self, actor, subject, shop_id=None, capability="learning_review", shops=None, quest=None, excluded=(), commit=False):
        from .authority import Authority
        return Authority(self).authorize(actor, subject, capability, shops or [shop_id], quest, excluded, commit)

    def touch(self, member_id):
        p = self.participant(member_id)
        if p:
            p["revision"] = p.get("revision", 0) + 1
            self.store.put("ledger_participants", p)

    def touch_preferences(self, member_id):
        p = self.preference_profile(member_id)
        p["revision"] = p.get("revision", 0) + 1
        self.save_preference_profile(p)

    def presentation(self, slot):
        ranks = self.store.get("ledger_catalog", "rank_display")["ranks"]
        return deepcopy(ranks[slot - 1]) if slot else {"slot": 0, "name": "Participant", "emoji": ""}

    def join(self, member_id, sponsor=None):
        return self.tx("_join", member_id, sponsor)

    def _join(self, member_id, sponsor):
        if (self.store.get("ledger_catalog", "control") or {}).get("paused"):
            raise Denied("The Ledger is paused for maintenance. Please try joining later.")
        if not self.sources.permitted(member_id):
            raise Denied("A valid makerspace account linked to an active Slack identity is required.")
        if self.is_ineligible(member_id):
            raise Denied("This Slack identity is not eligible for Ledger participation.")
        identity = self.store.get("ledger_catalog", f"identity:{member_id}") or {}
        if identity.get("deactivated") or identity.get("bot"):
            raise Denied("Your Slack identity is deactivated.")
        p = self.participant(member_id)
        if p and p["opted_in"]:
            return p
        first = not p
        if first:
            preferences = self.preference_profile(member_id)
            version = self.store.get("ledger_rulesets", "head")["version"]
            rules = self.store.get("ledger_rulesets", version)
            initial_rank = 1 if amount(rules["ranks"][0]["floor"]) == 0 else 0
            p = {"_id": member_id, "member_id": member_id, "ruleset": version, "xp": "0",
                 "rank": initial_rank, "first_opt_in": now(), "revision": 0, "metrics": {}, "import_pending": True}
            p.update({k: deepcopy(preferences[k]) for k in ("preferences", "observation_generation",
                "observation_notice_delivered_at", "observation_notice_ts") if k in preferences})
            # Serialize joining with nonparticipant preference/notice updates.
            preferences.update(kind="member_preferences_migrated", migrated_at=now())
            self.store.put("ledger_relationships", preferences)
        p.update(opted_in=True, import_pending=True, revision=p["revision"] + 1,
                 consent_generation=p.get("consent_generation", 0) + 1)
        p.setdefault("preferences", {"observation": True, "arrival_mentions": True})
        self.store.put("ledger_participants", p)
        self.store.put("ledger_evidence", {"_id": f"consent:{member_id}:{p['revision']}", "kind": "consent",
                       "member_id": member_id, "opted_in": True, "at": now(), "silent_accrual": True})
        if sponsor:
            rel = self.store.get("ledger_relationships", f"sponsor:{member_id}")
            from .sponsorships import invitation_key
            invited_by_sponsor = self.store.get("ledger_relationships", invitation_key(sponsor, member_id))
            attributed = rel and (rel.get("giver") == sponsor or invited_by_sponsor)
            if attributed:
                accepted_at = now()
                if rel["status"] == "pending":
                    rel.update(status="accepted", accepted_at=accepted_at)
                    self.store.put("ledger_relationships", rel)
                if invited_by_sponsor:
                    invited_by_sponsor.update(status="accepted", accepted_at=accepted_at,
                                              accepted_generation=p.get("consent_generation"))
                    self.store.put("ledger_relationships", invited_by_sponsor)
                # Keep canonical first-sponsor accounting, but notify the inviter
                # whose per-inviter invitation was actually accepted.
                inviter = sponsor if invited_by_sponsor else rel.get("giver")
                inviter_participant = self.participant(inviter) if isinstance(inviter, str) else None
                inviter_slack_id = self.sources.slack_id(inviter) if isinstance(inviter, str) else None
                from .sponsorship_reminder import queue_reminder_followup
                if inviter_participant and self.active(inviter) and inviter_slack_id:
                    queue_reminder_followup(self.store, inviter, "invitation_accepted", member_id,
                                            inviter_slack_id, acceptance_id=p.get("consent_generation"))
        from .admin_access import sync_review_membership
        sync_review_membership(self, member_id)
        self._invite(member_id, "chat", explicit=True)
        if p["rank"]:
            self._invite(member_id, f"rank:{p['rank']}", explicit=True)
        if first and p["rank"] == 1:
            enqueue(self.store, "ledger_outbox", f"art:welcome:{member_id}", "rank_art", {"member_id": member_id, "slot": 1,
                    "consent_generation": p["consent_generation"]})
        enqueue(self.store, "ledger_inbox", f"import:{member_id}:{p['revision']}", "reconcile_member", {"member_id": member_id, "historical": True})
        self.notify(member_id, "onboarding" if first else "return", {"summary": "Your skills and XP are retained and continue accruing silently if you opt out."}, f"join:{member_id}:{p['revision']}")
        from .engagement import Engagement
        Engagement(self).notice(member_id)
        from .avatars import request
        request(self, member_id, f"join:{p['consent_generation']}")
        return p

    def leave(self, member_id):
        return self.tx("_leave", member_id)

    def _leave(self, member_id):
        p = self.participant(member_id)
        if not p:
            return
        p.update(opted_in=False, revision=p["revision"] + 1)
        self.store.put("ledger_participants", p)
        from .avatars import cancel
        cancel(self, member_id)
        from .authority import Authority
        Authority(self).cleanup(member_id, "Consent withdrawn; a new grant is required after rejoining")
        from .admin_access import sync_review_membership
        sync_review_membership(self, member_id)
        self.store.put("ledger_evidence", {"_id": f"consent:{member_id}:{p['revision']}", "kind": "consent", "member_id": member_id, "opted_in": False, "at": now()})
        for job in self.store.select("ledger_outbox", {"status": {"$in": ["pending", "working"]}}):
            payload = job["payload"]
            if payload.get("member_id") == member_id and job["kind"] in ("invite", "message", "mqtt", "conversation", "welcome", "quest_draft",
                    "summary_flush", "summary_delivery", "guidance", "sponsor_report", "rank_art", "home_publish", "home_profile_photo") and not payload.get("peer_kudos"):
                job["status"] = "cancelled"
                self.store.put("ledger_outbox", job)
        for channel in self.store.select("ledger_channels", {"kind": "channel"}):
            enqueue(self.store, "ledger_outbox", f"leave:{member_id}:{p['revision']}:{channel['_id']}", "remove",
                    {"member_id": member_id, "channel": channel["channel_id"]})
        # Explicit acknowledgment is allowed after consent is withdrawn.
        self.notify(member_id, "opt_out", {"summary": "You have left game participation. Channel removal is queued. Skills and XP are retained; source activity continues accruing silently. Observation is a separate choice: use /ledger preferences and uncheck Allow observation to disable it."},
                    f"optout:{member_id}:{p['revision']}", exception=True)
        enqueue_home_refresh(self.store, member_id, f"leave:{p['revision']}", self.sources.slack_id(member_id))

    def _invite(self, member_id, channel_key, explicit=False, inviter=None, rank_transition=None):
        channel = self.store.get("ledger_channels", channel_key)
        if not channel or self.is_ineligible(member_id) or not self.active(member_id):
            return
        key = f"membership:{member_id}:{channel_key}"
        membership = self.store.get("ledger_channels", key) or {"_id": key, "kind": "membership", "member_id": member_id, "channel_key": channel_key}
        if membership.get("voluntary_leave") and not explicit:
            return
        membership.update(voluntary_leave=False, desired=True, inviter=inviter)
        self.store.put("ledger_channels", membership)
        p = self.participant(member_id)
        payload = {"member_id": member_id, "channel": channel["channel_id"],
                   "channel_key": channel_key, "revision": p["revision"]}
        if rank_transition:
            payload["rank_transition"] = deepcopy(rank_transition)
        enqueue(self.store, "ledger_outbox", f"invite:{member_id}:{channel_key}:{p['revision']}:{uuid4()}", "invite", payload)

    def invite(self, actor, target, channel_key):
        return self.tx("_peer_invite", actor, target, channel_key)

    def _peer_invite(self, actor, target, channel_key):
        self.require(actor)
        if self.is_ineligible(target):
            return
        p = self.require(target)
        self.touch(target)
        slot = int(channel_key.split(":")[1]) if channel_key.startswith("rank:") else 0
        if slot > p["rank"]:
            raise Denied("That rank has not been earned.")
        if actor == target and slot and slot != p["rank"]:
            raise Denied("Only your current rank channel can be restored through self-service.")
        if actor != target:
            m = self.store.get("ledger_channels", f"membership:{actor}:{channel_key}") or {}
            if not m.get("present"):
                raise Denied("Only a current member of that channel may invite someone back.")
        self._invite(target, channel_key, explicit=True, inviter=actor)

    def notify(self, member_id, kind, facts, key, exception=False, administrative=False, *, action_id=None):
        if self.is_ineligible(member_id):
            return
        if not exception and not self.active(member_id):
            return
        facts = dict(facts or {})
        deterministic_text = facts.pop("_deterministic_text", None)
        positive_xp = kind in RESULT_XP and amount(facts.get("xp_change", "0")) > 0
        verified_milestone = kind in CHALLENGES | {"quest"} and facts.get("verified_milestone") is True
        informational_unlock = kind == "quest" and str(key).startswith("quest-unlock:")
        if not exception and not administrative and (verified_milestone or kind in MAJOR):
            enqueue_home_refresh(self.store, member_id, str(action_id or key), self.sources.slack_id(member_id))
        if action_id and not exception and not administrative and (positive_xp or verified_milestone or kind in MAJOR or informational_unlock):
            from .result_summaries import collect
            return collect(self, action_id, member_id, kind, facts, str(key))
        payload = {"member_id": member_id, "type": kind, "audience": "member", "facts": facts,
                   "exception": exception, "administrative": administrative}
        if deterministic_text:
            payload["deterministic_text"] = deterministic_text
        enqueue(self.store, "ledger_outbox", f"dm:{key}", "message", payload)

    def major(self, member_id, kind, facts, key, historical=False, *, action_id=None):
        if historical or not self.active(member_id) or kind not in MAJOR:
            return
        facts = {**facts, "type": kind}
        summary_id = self.notify(member_id, kind, facts, key, action_id=action_id)
        # Rank transitions are announced only in the prior/new private rank
        # channels. The legacy shared renderer prepends the current rank.
        if kind != "rank_up":
            pending = [j for j in self.store.select("ledger_outbox", {"status": "pending"})
                       if j["kind"] == "message" and j["payload"].get("coalesce") == member_id]
            if pending:
                job = pending[0]
                job["payload"]["facts"]["achievements"].append(facts)
                self.store.put("ledger_outbox", job)
            else:
                enqueue(self.store, "ledger_outbox", f"shared:{key}", "message", {"member_id": member_id,
                    "type": kind, "audience": "shared", "coalesce": member_id, "facts": {"achievements": [facts]}}, delay=60)
        enqueue(self.store, "ledger_outbox", f"mqtt:{key}", "mqtt", {"member_id": member_id,
                "event_id": key, "type": kind, "achievement": facts, "ruleset": self.participant(member_id)["ruleset"], "occurred_at": now().isoformat()})
        return summary_id

    def award(self, member_id, source, desired, kind, historical=False, facts=None, *, action_id=None):
        """Set an evidence contribution; append only the delta, including reversals."""
        p = self.participant(member_id)
        if not p:
            return False
        key = f"account:{member_id}:{source}"
        prior = self.store.get("ledger_evidence", key) or {"_id": key, "kind": "account", "member_id": member_id, "source": source, "balance": "0", "revision": 0}
        delta = amount(desired) - amount(prior["balance"])
        if not delta:
            return False
        prior.update(balance=str(desired), revision=prior["revision"] + 1)
        self.store.put("ledger_evidence", prior)
        award_id = f"{key}:{prior['revision']}"
        self.store.put("ledger_awards", {"_id": award_id, "member_id": member_id, "kind": kind,
                        "source": source, "delta": str(delta), "at": now(), "ruleset": p["ruleset"]})
        p.update(xp=str(amount(p["xp"]) + delta), revision=p["revision"] + 1)
        self.store.put("ledger_participants", p)
        if not historical and kind != "kudos" and (delta <= 0 or kind not in ("boss", "stewardship") or action_id):
            self.notify(member_id, kind if delta > 0 else "correction", {**(facts or {}), "award_id": award_id,
                        "xp_change": str(delta), "xp_total": p["xp"]}, award_id, action_id=action_id)
        return True

    def kudos(self, giver, recipient, message, *, key, shop=None, tool=None, public=False, invite=False, expected_participation=None, emoji=None):
        return self.tx("_kudos", giver, recipient, message, key=key, shop=shop, tool=tool, public=public,
                       invite=invite, expected_participation=expected_participation, emoji=emoji)

    def _kudos(self, giver, recipient, message, *, key, shop, tool, public, invite, expected_participation, emoji):
        from .kudos import selected_emoji
        self.require_member(giver)
        existing = self.store.get("ledger_evidence", f"kudos:{key}")
        if existing:
            if existing["giver"] != giver:
                raise Denied("Submission belongs to another member.")
            return existing
        if giver == recipient:
            raise Denied("You cannot give yourself kudos.")
        from .kudos import require_recipient
        require_recipient(self, recipient)
        state = self.store.get("ledger_catalog", f"identity:{recipient}") or {}
        if state.get("deactivated") or state.get("bot"):
            raise Denied("Choose an active human Slack member.")
        if not isinstance(message, str) or not message.strip() or len(message) > 2000:
            raise ValueError("A kudos message of 1–2,000 characters is required.")
        participating = self.active(recipient)
        if expected_participation is None or expected_participation != participating:
            raise ConsentChanged("The recipient's participation changed. Please review the updated warning and invitation choice.")
        shop_doc = self.sources.shop(shop) if shop else None
        tool_doc = self.sources.tool(tool) if tool else None
        if shop and (not shop_doc or shop_doc.get("disabled")):
            raise ValueError("Select an available shop.")
        if tool and (not shop or not tool_doc or tool_doc.get("disabled") or sid(tool_doc["shop_id"]) != shop):
            raise ValueError("The tool must belong to the selected shop.")
        self.touch(giver)
        self.touch(recipient)
        local = now().astimezone(ZoneInfo("America/New_York"))
        day = local.date().isoformat()
        week = (local.date() - timedelta(days=local.weekday())).isoformat()
        pair_receipts = self.store.select("ledger_evidence", {"kind": "kudos", "recipient": recipient, "giver": giver, "week": week, "xp_awarded": True})
        daily_receipts = self.store.select("ledger_evidence", {"kind": "kudos", "recipient": recipient, "day": day, "xp_awarded": True})
        eligible = participating and not pair_receipts and len(daily_receipts) < 5
        # An excluded account may receive ordinary kudos, but the invitation
        # choice is silently suppressed and never becomes an invitation record.
        invite = bool(invite and not self.is_ineligible(giver) and not self.is_ineligible(recipient))
        evidence = {"_id": f"kudos:{key}", "kind": "kudos", "giver": giver, "recipient": recipient,
                    "message": message, "emoji": selected_emoji(emoji), "shop": shop, "tool": tool, "shop_name": (shop_doc or {}).get("name"),
                    "tool_name": (tool_doc or {}).get("name"), "public": bool(public), "invite": bool(invite),
                    "participating_at_submission": participating, "xp_awarded": bool(eligible),
                    "at": now(), "day": day, "week": week, "deliveries": {},
                    "result_summary": True, "action_id": f"kudos:{key}"}
        self.store.put("ledger_evidence", evidence)
        from .engagement import Engagement
        Engagement(self).capture(giver, evidence["_id"], "kudos_metadata", at=evidence["at"], metadata={"shop": shop, "tool": tool})
        if eligible:
            awarded = self.award(recipient, evidence["_id"], "17", "kudos", action_id=evidence["action_id"])
            progression_summary = self._advance(recipient, action_id=evidence["action_id"])
            # Keep ordinary recipient kudos as its original note. If it also
            # caused progression, that existing result must report the award.
            if awarded and progression_summary:
                account_key = f"account:{recipient}:{evidence['_id']}"
                account = self.store.get("ledger_evidence", account_key)
                award_id = f"{account_key}:{account['revision']}"
                award = self.store.get("ledger_awards", award_id)
                from .result_summaries import collect
                collect(self, evidence["action_id"], recipient, "kudos", {"award_id": award_id,
                        "xp_change": award["delta"], "xp_total": self.participant(recipient)["xp"]}, award_id)
        if invite and not participating and self.active(giver):
            self._sponsor(giver, recipient, notify=False, source="kudos_invitation")
        for audience in ["recipient"] + (["shared"] if public else []):
            enqueue(self.store, "ledger_outbox", f"{evidence['_id']}:{audience}", "kudos",
                    {"evidence": evidence["_id"], "audience": audience, "member_id": recipient, "peer_kudos": True})
        from .result_summaries import finish_action, track_kudos
        evidence["summary_id"] = track_kudos(self, evidence)
        self.store.put("ledger_evidence", evidence)
        finish_action(self, evidence["action_id"])
        return evidence

    def sponsor(self, giver, recipient, *, source="sponsor_command"):
        return self.tx("_sponsor", giver, recipient, source=source)

    def _sponsor(self, giver, recipient, notify=True, *, source="sponsor_command"):
        self.require(giver)
        if self.is_ineligible(recipient):
            return None
        if source == "kudos_invitation":
            from .kudos import require_recipient
            require_recipient(self, recipient)
        if giver == recipient or (source != "kudos_invitation" and not self.sources.good_standing(recipient)):
            raise Denied("Choose another linked member in good standing.")
        from .sponsorships import invitation_key
        invitation_id = invitation_key(giver, recipient)
        invitation = self.store.get("ledger_relationships", invitation_id)
        key = f"sponsor:{recipient}"
        existing = self.store.get("ledger_relationships", key)
        if invitation:
            return existing or invitation
        if self.active(recipient):
            raise Denied("That member cannot receive a sponsorship invitation.")
        self.touch(giver)
        invited_at = now()
        if existing:
            rel = existing
        else:
            rel = {"_id": key, "kind": "sponsor", "giver": giver, "recipient": recipient,
                   "status": "pending", "at": invited_at}
            self.store.put("ledger_relationships", rel)
        self.store.put("ledger_relationships", {"_id": invitation_id, "kind": "sponsor_invitation",
            "giver": giver, "recipient": recipient, "at": invited_at, "source": source})
        if notify:
            self.notify(recipient, "invitation", {"sponsor": giver}, invitation_id, exception=True)
        return rel

    def _recruitment(self, member_id, at, *, action_id=None):
        p = self.participant(member_id)
        rel = self.store.get("ledger_relationships", f"sponsor:{member_id}")
        if not p or not rel or rel["status"] != "accepted" or rel.get("rewarded") or not at or at <= p["first_opt_in"]:
            return
        self.award(rel["giver"], f"recruit:{member_id}", "11", "recruitment", action_id=action_id)
        rel["rewarded"] = True
        self.store.put("ledger_relationships", rel)

    def publish_ranks(self, actor, ranks, expected=None):
        return self.tx("_publish_ranks", actor, ranks, expected)

    def _publish_ranks(self, actor, ranks, expected=None):
        self.admin(actor)
        head = self.store.get("ledger_rulesets", "head")
        if expected is not None and head["version"] != expected:
            raise ValueError("Ranks changed while you were editing. Reopen the editor to review the latest version.")
        rules = deepcopy(self.store.get("ledger_rulesets", head["version"]))
        rules.update(_id=str(uuid4()), ranks=deepcopy(ranks), actor=actor, at=now(), previous=head["version"])
        validate_rules(rules)
        self.store.put("ledger_rulesets", rules)
        head["version"] = rules["_id"]
        self.store.put("ledger_rulesets", head)
        self.store.put("ledger_catalog", {"_id": "rank_display", "ranks": deepcopy(ranks), "version": rules["_id"]})
        for rank in ranks:
            if rank["enabled"] and not self.store.get("ledger_channels", f"rank:{rank['slot']}"):
                enqueue(self.store, "ledger_outbox", f"provision:{rank['slot']}", "provision_slot", {"slot": rank["slot"]})
        return rules

    def rollback_ranks(self, actor, version):
        old = self.store.get("ledger_rulesets", version)
        if not old or "ranks" not in old:
            raise ValueError("Unknown progression version")
        return self.publish_ranks(actor, old["ranks"])

    def _advance(self, member_id, historical=False, *, action_id=None):
        p = self.participant(member_id)
        if not p or not self.sources.permitted(member_id):
            return
        identity = self.store.get("ledger_catalog", f"identity:{member_id}") or {}
        if identity.get("deactivated"):
            return
        if not self.sources.eligible_for_rank(member_id, self.store.get("ledger_evidence", f"coverage:{member_id}")):
            return
        rules = self.store.get("ledger_rulesets", p["ruleset"])
        rank = attainable_rank(rules, p["xp"], p.get("metrics", {}))
        if p.get("rank_hold"):
            return
        if rank <= p["rank"]:
            return
        old_slot = p["rank"]
        for slot in range(old_slot + 1, rank + 1):
            display = self.presentation(slot)
            self.store.put("ledger_awards", {"_id": f"rank:{member_id}:{slot}:{p['revision']}", "kind": "rank", "member_id": member_id,
                            "slot": slot, "name": display["name"], "emoji": display["emoji"], "ruleset": p["ruleset"], "at": now()})
        p.update(rank=rank, revision=p["revision"] + 1)
        self.store.put("ledger_participants", p)
        from .quests import Quests
        Quests(self).unlock_notice(member_id, action_id=action_id if not historical else None)
        transition = None if historical else {"old_slot": old_slot, "new_slot": rank,
            "consent_generation": p.get("consent_generation", 0)}
        self._invite(member_id, f"rank:{rank}", rank_transition=transition)
        summary_id = self.major(member_id, "rank_up", {"rank": self.presentation(rank)["name"], "slot": rank,
                   "old_slot": old_slot, "old_rank": self.presentation(old_slot)["name"],
                   "new_slot": rank, "new_rank": self.presentation(rank)["name"]}, f"rank:{member_id}:{rank}:{p['revision']}", historical, action_id=action_id)
        if not historical and self.active(member_id) and rank <= 6:
            enqueue(self.store, "ledger_outbox", f"art:{member_id}:{rank}:{p['revision']}", "rank_art", {"member_id": member_id, "slot": rank,
                    "summary_id": summary_id, "consent_generation": p.get("consent_generation", 0)})
        if not historical:
            from .avatars import request
            request(self, member_id, f"rank:{rank}:{p['revision']}")
        return summary_id

    def correct_rank(self, actor, member_id, slot, reason):
        return self.tx("_correct_rank", actor, member_id, slot, reason)

    def _correct_rank(self, actor, member_id, slot, reason):
        self.admin(actor)
        p = self.participant(member_id)
        if actor == member_id or not reason.strip() or not p or not 0 <= slot <= p["rank"]:
            raise ValueError("Provide an independent correction, a reason, and an already-held or lower rank.")
        self.store.put("ledger_awards", {"_id": str(uuid4()), "kind": "rank_correction", "actor": actor,
                       "member_id": member_id, "before": p["rank"], "after": slot, "reason": reason, "at": now()})
        rank_changed = p["rank"] != slot
        p.update(rank=slot, rank_hold=True, revision=p["revision"] + 1)
        if rank_changed:
            p["avatar_generation"] = p.get("avatar_generation", 0) + 1
        self.store.put("ledger_participants", p)
        enqueue_home_refresh(self.store, member_id, f"rank-correction:{p['revision']}", self.sources.slack_id(member_id))
        if rank_changed:
            from .avatars import cancel, request
            cancel(self, member_id)
            request(self, member_id, f"rank-correction:{p['revision']}")
        from .quests import Quests
        Quests(self).cleanup(member_id)
        self.notify(member_id, "correction", {"summary": reason, "rank": self.presentation(slot)["name"]}, str(uuid4()))

    def release_rank(self, actor, member_id, reason, *, action_id=None):
        action_id = action_id or "rank-release:" + str(uuid4())
        def release(s):
            d = Ledger(s, self.sources)
            d.reviewer(actor, member_id)
            d.admin(actor)
            p = d.participant(member_id)
            if not p or not reason.strip():
                raise ValueError("Supply a participant and resolution reason.")
            p.update(rank_hold=False, revision=p["revision"] + 1)
            s.put("ledger_participants", p)
            s.put("ledger_awards", {"_id": str(uuid4()), "kind": "rank_hold_release", "member_id": member_id, "actor": actor, "reason": reason, "at": now()})
            d._advance(member_id, action_id=action_id)
            from .result_summaries import finish_action
            finish_action(d, action_id)
        self.store.atomic(release)

    def publish_catalog(self, actor, doc):
        self.admin(actor)
        if doc.get("kind") not in ("shop_completion", "challenge") or not doc.get("_id"):
            raise ValueError("Catalog entries require an id and a supported kind.")
        if doc["kind"] == "shop_completion":
            shop = self.sources.shop(doc.get("shop_id"))
            if not shop or shop.get("disabled"):
                raise ValueError("Choose an enabled shop.")
            tools = self.sources.rows("tools", {"shop_id": shop["_id"]})
            doc["tool_ids"] = [sid(t["_id"]) for t in tools if not t.get("disabled") and not t.get("open")]
            if not doc["tool_ids"]:
                raise ValueError("An empty shop cannot be a completion milestone.")
        elif doc.get("achievement") not in CHALLENGES or not doc.get("criteria", "").strip():
            raise ValueError("Specify an achievement and observable acceptance criteria.")
        if doc.get("achievement") in ("boss", "stewardship") and not doc.get("task_id"):
            raise ValueError("Major volunteer milestones must reference a volunteer opportunity.")
        if doc.get("task_id") and not self.sources.rows("volunteer_tasks", {"_id": object_id(doc["task_id"])}):
            raise ValueError("Volunteer opportunity was not found.")
        doc = deepcopy(doc)
        doc.update(actor=actor, at=now(), active=True)
        def write(s):
            if s.get("ledger_catalog", doc["_id"]):
                raise ValueError("Catalog versions are immutable. Use a new id for a revised set.")
            s.put("ledger_catalog", doc)
            if doc["kind"] == "shop_completion":
                s.put("ledger_catalog", {"_id": f"shop-head:{doc['shop_id']}", "version": doc["_id"]})
        self.store.atomic(write)
        return doc

    def submit(self, member_id, catalog_id, description, learners=None, shop=None, mentor=None, handoff=None, key=None):
        return self.tx("_submit", member_id, catalog_id, description, learners or [], shop, mentor, handoff, key)

    def _submit(self, member_id, catalog_id, description, learners, shop, mentor, handoff, key):
        self.require(member_id)
        evidence_key = f"submission:{member_id}:{key}" if key else str(uuid4())
        previous = self.store.get("ledger_evidence", evidence_key)
        if previous:
            return previous
        catalog = self.store.get("ledger_catalog", catalog_id)
        if not catalog or catalog.get("kind") != "challenge" or not catalog.get("active") or not description.strip():
            raise ValueError("Choose a published challenge and describe your evidence.")
        kind = catalog["achievement"]
        if shop and not self.sources.shop(shop):
            raise ValueError("Unknown shop.")
        if kind == "mentoring" and (not learners or member_id in learners):
            raise ValueError("A mentoring session needs at least one other learner.")
        if kind == "develop_mentor" and (not mentor or mentor == member_id):
            raise ValueError("Identify the other member you helped develop as a mentor.")
        if kind == "stewardship" and not (handoff or "").strip():
            raise ValueError("Stewardship requires a usable handoff: documentation, location, and who can take over.")
        doc = {"_id": evidence_key, "kind": "submission", "achievement": kind, "catalog_id": catalog_id,
               "member_id": member_id, "description": description, "learners": list(set(learners)),
               "acknowledged": [], "shop_id": catalog.get("shop_id") or shop, "mentor": mentor, "handoff": handoff,
               "status": "pending", "at": now()}
        doc["shop_ids"] = sorted(set(catalog.get("shop_ids", [])) | ({doc["shop_id"]} if doc["shop_id"] else set()))
        self.store.put("ledger_evidence", doc)
        for learner in learners:
            self.notify(learner, "mentoring", {"summary": "Please acknowledge the mentoring session.", "submission": doc["_id"]}, f"ack:{doc['_id']}:{learner}", exception=True)
        return doc

    def acknowledge(self, actor, evidence_id):
        def update(s):
            doc = s.get("ledger_evidence", evidence_id)
            if not doc or actor not in doc.get("learners", []):
                raise Denied("Only a listed learner can acknowledge this session.")
            doc["acknowledged"] = list(set(doc["acknowledged"] + [actor]))
            s.put("ledger_evidence", doc)
        self.store.atomic(update)

    def review(self, actor, evidence_id, approve=True, reason="", *, action_id=None):
        action_id = action_id or "review:" + str(uuid4())
        def run(s):
            d = Ledger(s, self.sources)
            result = d._review(actor, evidence_id, approve, reason, action_id=action_id)
            from .result_summaries import finish_action
            finish_action(d, action_id)
            return result
        return self.store.atomic(run)

    def _review(self, actor, evidence_id, approve, reason, quest_review=False, *, action_id=None):
        doc = self.store.get("ledger_evidence", evidence_id)
        if not doc or doc.get("kind") != "submission":
            raise ValueError("Unknown submission.")
        if doc.get("quest_link") and not quest_review:
            raise Denied("Review the linked quest completion so milestone and reward finalize together.")
        from .authority import evidence_capability
        audit = self.reviewer(actor, doc["member_id"], doc.get("shop_id"), evidence_capability(doc),
                              shops=doc.get("shop_ids"), quest=doc.get("quest_link"), commit=True)
        if doc["status"] != "pending":
            self.admin(actor)
        if doc.get("quest_link"):
            quest = self.store.get("ledger_quests", doc["quest_link"])
            if quest and actor == quest["creator"]:
                raise Denied("Authors cannot review evidence for their own quests.")
        if approve and doc["achievement"] == "mentoring" and set(doc["learners"]) != set(doc["acknowledged"]):
            raise ValueError("All listed learners must acknowledge the session first.")
        if approve and doc["achievement"] == "develop_mentor":
            guidance = [r for r in self.store.select("ledger_evidence", {"kind": "submission", "member_id": doc["member_id"], "status": "approved"})
                        if r.get("achievement") == "mentoring" and doc["mentor"] in r.get("learners", [])]
            teaching = self.store.select("ledger_evidence", {"kind": "submission", "member_id": doc["mentor"], "status": "approved"})
            valid = any(t.get("achievement") == "mentoring" and t.get("reviewer") not in (doc["member_id"], doc["mentor"])
                        and t["at"] > g["at"] for g in guidance for t in teaching)
            if not valid:
                raise ValueError("Record verified guidance, followed by the learner's teaching session independently verified by another reviewer.")
        if not approve and not reason.strip():
            raise ValueError("A correction or rejection requires a reason.")
        self.store.put("ledger_awards", {"_id": str(uuid4()), "kind": "review", "evidence": evidence_id, "actor": actor,
                       "before": doc["status"], "after": "approved" if approve else "rejected", "reason": reason, "at": now(), **audit})
        new_verification = approve and doc["status"] != "approved"
        # A review may finalize before the first silent import. Snapshot only
        # this reviewed milestone's account, so unrelated catch-up stays silent.
        account_key = f"account:{doc['member_id']}:challenge:{doc['catalog_id']}"
        prior_account = self.store.get("ledger_evidence", account_key) or {}
        doc.update(status="approved" if approve else "rejected", reviewer=actor, reviewed_at=now(), reason=reason)
        self.store.put("ledger_evidence", doc)
        self._reconcile(doc["member_id"], action_id=action_id)
        if new_verification and doc["achievement"] in CHALLENGES:
            from .avatars import request
            request(self, doc["member_id"], "verified:" + evidence_id)
        if action_id and new_verification and doc["achievement"] in CHALLENGES:
            catalog = self.store.get("ledger_catalog", doc["catalog_id"]) or {}
            reviewed_result = {"verified_milestone": True, "milestone_id": evidence_id, "xp_outcome_known": True,
                               "challenge_title": catalog.get("title"), "summary": "Independently verified milestone."}
            current_account = self.store.get("ledger_evidence", account_key) or {}
            if (not doc.get("quest_link") and doc["achievement"] != "mentoring" and
                    current_account.get("revision", 0) != prior_account.get("revision", 0)):
                award_id = f"{account_key}:{current_account['revision']}"
                reviewed_award = self.store.get("ledger_awards", award_id) or {}
                if amount(reviewed_award.get("delta", "0")) > 0:
                    reviewed_result.update(award_id=award_id, xp_change=reviewed_award["delta"],
                                           xp_total=self.participant(doc["member_id"])["xp"])
            self.notify(doc["member_id"], doc["achievement"], reviewed_result,
                        "verified:" + evidence_id, action_id=action_id)
        return doc

    def reconcile(self, member_id, historical=False, *, action_id=None):
        # Generate outside the retryable callback, so a Mongo transaction retry
        # cannot split one result across multiple summary owners.
        action_id = action_id or "reconcile:" + str(uuid4())
        def run(s):
            d = Ledger(s, self.sources)
            result = d._reconcile(member_id, historical, action_id=action_id)
            from .result_summaries import finish_action
            finish_action(d, action_id)
            return result
        return self.store.atomic(run)

    def _reconcile(self, member_id, historical=False, *, action_id=None):
        from .admin_access import sync_review_membership
        sync_review_membership(self, member_id)
        from .authority import Authority
        from .quests import Quests
        Authority(self).cleanup(member_id)
        Quests(self).cleanup(member_id)
        from .engagement import Engagement
        Engagement(self).notice(member_id)
        if (self.store.get("ledger_catalog", "control") or {}).get("paused"):
            return
        p = self.participant(member_id)
        if not p:
            return
        historical = historical or p.get("import_pending", False)
        rules = self.store.get("ledger_rulesets", p["ruleset"])
        rates = rules["xp"]
        desired, details, learner_ids = {}, {}, set()
        metrics = {"checkouts": 0, "shops": 0, "completed_shops": 0, "mentoring": 0, "learners": 0, "volunteer": "0"}
        checkouts = self.sources.rows("tool_checkouts", {"member_id": object_id(member_id)})
        active = {}
        for checkout in sorted(checkouts, key=lambda c: c.get("checked_out_at") or p["first_opt_in"] - timedelta(days=36500)):
            if not checkout.get("revoked_at"):
                active.setdefault(sid(checkout["tool_id"]), checkout)
        shops = set()
        for tool_id, checkout in active.items():
            tool = self.sources.tool(tool_id)
            if not tool or tool.get("open"):
                continue
            # Freeze a tool's XP classification on first observation, not on later catalog edits.
            class_key = f"checkout_rate:{member_id}:{tool_id}"
            classification = self.store.get("ledger_evidence", class_key)
            if not classification:
                classification = {"_id": class_key, "kind": "checkout_rate", "rate": rates["checkout" if tool.get("prerequisite_ids") else "basic_checkout"]}
                self.store.put("ledger_evidence", classification)
            desired[f"checkout:{tool_id}"] = (classification["rate"], "checkout_earned")
            details[f"checkout:{tool_id}"] = {"tool_name": tool["name"], "shop_name": (self.sources.shop(tool.get("shop_id")) or {}).get("name")}
            metrics["checkouts"] += 1
            shops.add(sid(tool.get("shop_id")))
            if not historical:
                self._recruitment(member_id, checkout.get("checked_out_at"), action_id=action_id)
        metrics["shops"] = len(shops - {None})
        taught = self.sources.rows("tool_checkouts", {"approved_by_id": object_id(member_id)})
        active_taught = [c for c in taught if not c.get("revoked_at") and c.get("member_id") and sid(c.get("member_id")) != member_id
                        and self.sources.tool(sid(c.get("tool_id"))) and not self.sources.tool(sid(c.get("tool_id"))).get("open")]
        for checkout in active_taught:
            desired[f"teaching:{checkout['_id']}"] = (rates["teaching"], "checkout_granted")
            tool = self.sources.tool(checkout["tool_id"])
            details[f"teaching:{checkout['_id']}"] = {"tool_name": tool["name"], "shop_name": (self.sources.shop(tool.get("shop_id")) or {}).get("name")}
            learner_ids.add(sid(checkout.get("member_id")))
        metrics["mentoring"] = len(active_taught)
        credits = self.sources.rows("volunteer_credits", {"member_id": object_id(member_id)})
        # Each original credit has one effective balance. Reversal metadata and offset records
        # describe the same correction; do not subtract them twice.
        reversed_ids = {sid(c.get("reversal_of_id")) for c in credits if c.get("status") == "reversal"}
        linked_credit_ids = {sid(c.get("volunteer_credit_id")) for c in taught}
        volunteer = Decimal(0)
        for credit in credits:
            if credit.get("status") != "approved" or credit.get("reversed") or sid(credit["_id"]) in reversed_ids:
                continue
            value = amount(credit.get("credit_value", 0))
            volunteer += value
            if not credit.get("tool_checkout_id") and sid(credit["_id"]) not in linked_credit_ids:
                desired[f"credit:{credit['_id']}"] = (value * amount(rates["volunteer"]), "volunteer_credit")
                tasks = self.sources.rows("volunteer_tasks", {"_id": object_id(credit["task_id"])}) if credit.get("task_id") else []
                details[f"credit:{credit['_id']}"] = {"volunteer_credits": str(value), "challenge_title": tasks[0].get("title") if tasks else None}
            if not historical:
                self._recruitment(member_id, credit.get("created_at"), action_id=action_id)
                if credit.get("created_at"):
                    Engagement(self).capture(member_id, f"volunteer:{credit['_id']}", "volunteer", at=credit["created_at"], metadata={"credit_value": str(value)})
        metrics["volunteer"] = str(volunteer)
        approved = self.store.select("ledger_evidence", {"kind": "submission", "member_id": member_id, "status": "approved"})
        used = set()
        for doc in sorted(approved, key=lambda x: x["at"]):
            kind = doc["achievement"]
            # Mentoring sessions repeat; learning challenges are unique per member/catalog.
            unique = doc["_id"] if kind == "mentoring" else doc["catalog_id"]
            if unique in used:
                continue
            used.add(unique)
            if kind == "mentoring":
                metrics["mentoring"] += 1
                learner_ids.update(doc["learners"])
            else:
                metrics[kind] = metrics.get(kind, 0) + 1
                rate = rates.get(kind, rates["challenge"])
                if not doc.get("quest_link"):
                    desired[f"challenge:{unique}"] = (rate, kind)
                catalog = self.store.get("ledger_catalog", doc["catalog_id"]) or {}
                details[f"challenge:{unique}"] = {"challenge_title": catalog.get("title")}
            if not historical:
                self._recruitment(member_id, doc["at"], action_id=action_id)
        metrics["learners"] = len(learner_ids - {None})
        # Freeze completed sets, so adding tools later never revokes a completed-shop milestone.
        for catalog in self.store.select("ledger_catalog", {"kind": "shop_completion"}):
            head = self.store.get("ledger_catalog", f"shop-head:{catalog['shop_id']}")
            if head and head["version"] != catalog["_id"]:
                continue
            achievement_id = f"shop:{member_id}:{catalog['shop_id']}"
            if catalog.get("tool_ids") and set(catalog["tool_ids"]).issubset(active) and not self.store.get("ledger_awards", achievement_id):
                self.store.put("ledger_awards", {"_id": achievement_id, "kind": "shop_completion", "member_id": member_id,
                    "catalog_id": catalog["_id"], "shop_id": catalog["shop_id"], "at": now()})
                if not historical:
                    from .avatars import request
                    request(self, member_id, "shop:" + achievement_id)
                self.major(member_id, "shop_complete", {"shop": (self.sources.shop(catalog["shop_id"]) or {}).get("name", "Shop")}, achievement_id, historical, action_id=action_id)
        metrics["completed_shops"] = len(self.store.select("ledger_awards", {"kind": "shop_completion", "member_id": member_id}))
        for source, (value, kind) in desired.items():
            changed = self.award(member_id, source, value, kind, historical, facts=details.get(source), action_id=action_id)
            if changed and kind in ("boss", "stewardship"):
                self.major(member_id, kind, {"milestone": kind, **details.get(source, {})}, f"{member_id}:{source}", historical, action_id=action_id)
        for account in self.store.select("ledger_evidence", {"kind": "account", "member_id": member_id}):
            source = account["source"]
            if source.startswith(("checkout:", "teaching:", "credit:", "challenge:")) and source not in desired:
                self.award(member_id, source, "0", "correction", historical, action_id=action_id)
        p = self.participant(member_id)
        p["metrics"] = metrics
        p["last_reconciled"] = now()
        p["import_pending"] = False
        self.store.put("ledger_participants", p)
        self._advance(member_id, historical, action_id=action_id)
        Quests(self).unlock_notice(member_id, action_id=action_id if not historical else None)

    def preferences(self, member_id, observation, arrival_mentions, *, avatars=None, reference_file=None, remove_reference=False):
        def run(s):
            d = Ledger(s, self.sources)
            if not d.member_eligible(member_id):
                raise Denied("A valid linked human Slack account is required.")
            p = d.preference_profile(member_id)
            if type(observation) is not bool or type(arrival_mentions) is not bool:
                raise ValueError("Choose both preferences explicitly.")
            preferences = {**p.get("preferences", {}), "observation": observation, "arrival_mentions": arrival_mentions}
            previous_avatars = preferences.get("avatars", True)
            observation_changed = observation != p.get("preferences", {}).get("observation", True)
            if avatars is not None:
                if type(avatars) is not bool:
                    raise ValueError("Choose the avatar preference explicitly.")
                preferences["avatars"] = avatars
            if previous_avatars != preferences.get("avatars", True) or remove_reference or observation_changed:
                p["avatar_generation"] = p.get("avatar_generation", 0) + 1
            if reference_file or remove_reference:
                p["avatar_reference_revision"] = p.get("avatar_reference_revision", 0) + 1
            p.update(preferences=preferences, revision=p["revision"] + 1,
                     observation_generation=p.get("observation_generation", 0) + 1)
            d.save_preference_profile(p)
            from .avatars import cancel, request, retire
            if not preferences.get("avatars", True) or remove_reference or observation_changed:
                cancel(d, member_id)
            if not preferences.get("avatars", True):
                retire(d, member_id)
            if remove_reference:
                # Keep the user-owned Slack file; clear only the active reference.
                s.put("ledger_avatars", {"_id": "reference:" + member_id, "kind": "reference_removed", "member_id": member_id})
            if previous_avatars != preferences.get("avatars", True) or remove_reference or observation_changed:
                enqueue_home_refresh(s, member_id, f"avatar-preferences:{p['revision']}")
                request(d, member_id, f"preferences:{p['revision']}")
            if reference_file and preferences.get("avatars", True) and d.active(member_id):
                enqueue(s, "ledger_outbox", f"avatar-reference:{member_id}:{p['revision']}", "avatar_reference",
                        {"member_id": member_id, "file_id": reference_file, "consent_generation": p.get("consent_generation", 0),
                         "avatar_generation": p.get("avatar_generation", 0), "reference_revision": p["avatar_reference_revision"]})
            s.put("ledger_evidence", {"_id": "preferences:" + str(uuid4()), "kind": "preferences", "member_id": member_id,
                "preferences": p["preferences"], "at": now()})
            if observation:
                from .engagement import Engagement
                Engagement(d).notice(member_id)
            return p
        return self.store.atomic(run)

    def coverage(self, actor, member_id, reason):
        self.admin(actor)
        if actor == member_id or not reason.strip():
            raise Denied("Coverage needs independent verification and a reason.")
        m = self.sources.member(member_id)
        if not m or not m.get("expirationTime"):
            raise ValueError("The member has no recorded expiration date.")
        self.store.atomic(lambda s: s.put("ledger_evidence", {"_id": f"coverage:{member_id}", "kind": "coverage",
            "member_id": member_id, "actor": actor, "reason": reason, "expiration": m["expirationTime"], "approved": True, "at": now()}))
        self.reconcile(member_id)
