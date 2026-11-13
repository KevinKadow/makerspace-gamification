"""Human-reviewed Ledger proposals and a single cooperative project per quest."""
from copy import deepcopy
from uuid import uuid4

from .authority import Authority
from .domain import Denied, Ledger
from .quest_policy import (DEFINITION_FIELDS, LEDGER_AUTHOR, contains_rank_name, cooperative, enabled_rank,
                           generated, minimum_rank, validate_definition)
from .rules import amount
from .storage import now


class LedgerQuests:
    def __init__(self, ledger):
        self.l = ledger

    def project(self, q):
        return self.l.store.get("ledger_relationships", "cooperative:" + q["logical_id"])

    def available(self, member, q):
        from .quests import Quests
        participant = self.l.require(member)
        state = self.project(q) if cooperative(q) else None
        if (not cooperative(q) or q["status"] != "published" or not enabled_rank(self.l, q["target_rank"])
                or (generated(q) and contains_rank_name(self.l, q))
                or not state or state["quest_revision"] != q["_id"] or state["status"] != "open"
                or not Quests(self.l).prerequisites(q, member)):
            raise Denied("This cooperative quest is unavailable or its prerequisites are not met.")
        rank_ineligible = (participant["rank"] < q["target_rank"] if minimum_rank(q)
                           else participant["rank"] != q["target_rank"])
        if not generated(q) and (rank_ineligible or not Quests(self.l).author_available(q)):
            raise Denied("This cooperative quest's minimum rank or proposer eligibility is not met.")
        return q

    def review(self, actor, key, reward, approve=True, reason="", edits=None):
        def run(s):
            d = Ledger(s, self.l.sources)
            q = s.get("ledger_quests", key)
            if not q or not generated(q):
                raise ValueError("Choose a Ledger-authored quest proposal.")
            receipt = s.get("ledger_evidence", "quest-review:" + key)
            if receipt:
                Authority(d).authorize(actor, LEDGER_AUTHOR, "quest_publish", q["shop_ids"], q["logical_id"])
                reviewed = s.get("ledger_quests", receipt["quest"])
                Authority(d).authorize(actor, LEDGER_AUTHOR, "quest_publish", reviewed["shop_ids"], q["logical_id"])
                return reviewed
            if q["status"] != "pending_review":
                raise ValueError("Choose a pending Ledger quest revision.")
            if approve and not enabled_rank(d, q["target_rank"]):
                raise Denied("The target rank is no longer enabled.")
            if type(reward) is not int or not 0 <= reward <= 500:
                raise ValueError("Quest rewards must be whole numbers from 0 to 500 XP.")
            if not approve and not reason.strip():
                raise ValueError("Rejection requires a reason.")
            # Both original and edited scope need coverage; the model cannot pick
            # its reviewer or turn an unscoped quest into a narrower authority grant.
            Authority(d).authorize(actor, LEDGER_AUTHOR, "quest_publish", q["shop_ids"], q["logical_id"])
            original = {k: q[k] for k in DEFINITION_FIELDS}
            definition = validate_definition(d, edits if edits is not None else original, q["quest_type"]) if approve else original
            audit = Authority(d).authorize(actor, LEDGER_AUTHOR, "quest_publish", definition["shop_ids"], q["logical_id"], commit=True)
            reviewed = q
            if approve and definition != {k: q[k] for k in DEFINITION_FIELDS}:
                reviewed = {**deepcopy(q), **definition, "_id": str(uuid4()), "revision_of": key,
                            "edited_by": actor, "edited_at": now()}
                q.update(status="superseded", superseded_by=reviewed["_id"])
                s.put("ledger_quests", q)
            reviewed.update(status="published" if approve else "rejected", reward=reward,
                            classification="challenge", catalog_id=None, reviewer=actor,
                            reviewed_at=now(), review_authority=audit, reason=reason)
            s.put("ledger_quests", reviewed)
            receipt = {"_id": "quest-review:" + key, "kind": "quest_review", "proposal": key,
                       "quest": reviewed["_id"], "actor": actor, **audit, "at": now(),
                       "reason": reason, "status": reviewed["status"]}
            s.put("ledger_evidence", receipt)
            if approve:
                from .quest_discovery import quest_head
                s.put("ledger_catalog", quest_head(reviewed))
                if cooperative(reviewed):
                    project_id = "cooperative:" + q["logical_id"]
                    if s.get("ledger_relationships", project_id):
                        raise ValueError("This logical quest already has a shared project.")
                    s.put("ledger_relationships", {"_id": project_id, "kind": "quest_project",
                        "logical_id": q["logical_id"], "quest_revision": reviewed["_id"],
                        "status": "open", "contributions": {}, "at": now()})
            return reviewed
        return self.l.store.atomic(run)

    def contribute(self, actor, key, action, role=None, description=None, member=None):
        def run(s):
            d = Ledger(s, self.l.sources)
            service = LedgerQuests(d)
            q = s.get("ledger_quests", key)
            if not q or not cooperative(q):
                raise ValueError("Choose a cooperative Ledger quest.")
            if q["status"] != "published" or not enabled_rank(d, q["target_rank"]):
                raise Denied("This cooperative quest is unavailable.")
            state = service.project(q)
            if not state or state["status"] != "open" or state["quest_revision"] != key:
                raise Denied("This shared project is closed.")
            contributions = state["contributions"]
            if action == "verify":
                if actor in contributions:
                    raise Denied("Contributors cannot verify their own shared project.")
                service.available(member, q)
                audit = Authority(d).authorize(actor, member, "quest_complete", q["shop_ids"],
                                               q["logical_id"], excluded=set(contributions) | {q["creator"]}, commit=True)
                contribution = contributions.get(member)
                if not contribution or contribution["status"] != "pending":
                    raise ValueError("No submitted contribution to verify.")
                contribution.update(status="verified", reviewer=actor, review_authority=audit, verified_at=now())
                s.put("ledger_evidence", {"_id": f"group-review:{q['logical_id']}:{member}",
                    "kind": "quest_contribution_review", "quest_revision": key, "member_id": member,
                    "actor": actor, **audit, "description": contribution["description"],
                    "activity_at": contribution["submitted_at"], "at": now()})
            else:
                service.available(actor, q)
                d.touch(actor)
                if action == "join":
                    if role not in {r["name"] for r in q["disciplines"]}:
                        raise ValueError("Choose one of the predefined disciplines.")
                    contributions.setdefault(actor, {"role": role, "status": "joined", "joined_at": now(),
                        "rank": d.participant(actor)["rank"], "reward": q["reward"], "quest_revision": key})
                elif action == "submit":
                    if actor not in contributions or not isinstance(description, str) or not description.strip() or len(description) > 2000:
                        raise ValueError("Join first, then describe observable contribution evidence within 2,000 characters.")
                    if contributions[actor]["status"] == "verified":
                        raise ValueError("Verified contribution evidence is immutable.")
                    contributions[actor].update(description=description, status="pending", submitted_at=now())
                else:
                    raise ValueError("Unknown cooperative quest action.")
            # Write the revision to serialize project actions with disable.
            q["interaction_revision"] = q.get("interaction_revision", 0) + 1
            s.put("ledger_quests", q)
            s.put("ledger_relationships", state)
            return state
        return self.l.store.atomic(run)

    def verified_contributors(self, q, state):
        from .quests import Quests
        service = Quests(self.l)
        def eligible(member):
            participant = self.l.participant(member)
            return bool(self.l.active(member) and service.prerequisites(q, member)
                and (generated(q) or (participant and service.author_available(q)
                    and (participant["rank"] >= q["target_rank"] if minimum_rank(q)
                         else participant["rank"] == q["target_rank"]))))

        return {m: c for m, c in state["contributions"].items()
                if c["status"] == "verified" and eligible(m)}

    def finalize(self, actor, key, description, *, action_id=None):
        action_id = action_id or "group-completion:" + str(uuid4())
        def run(s):
            d = Ledger(s, self.l.sources)
            q = s.get("ledger_quests", key)
            if not q or not cooperative(q):
                raise ValueError("Choose a cooperative Ledger quest.")
            state = LedgerQuests(d).project(q)
            if not state:
                raise ValueError("The shared project is unavailable.")
            excluded = list(state["contributions"])
            authority = Authority(d).authorize(actor, q["creator"], "quest_complete", q["shop_ids"],
                                                q["logical_id"], excluded=set(excluded) | {q["creator"]}, commit=True)
            if state["status"] == "completed":
                return state
            if (state["status"] != "open" or q["status"] != "published" or not enabled_rank(d, q["target_rank"])
                    or (generated(q) and contains_rank_name(d, q))):
                raise Denied("This shared project is closed or disabled.")
            if not isinstance(description, str) or not description.strip() or len(description) > 2000:
                raise ValueError("Provide observable evidence of the shared outcome within 2,000 characters.")
            eligible = LedgerQuests(d).verified_contributors(q, state)
            if len(eligible) < 2 or not {r["name"] for r in q["disciplines"]}.issubset({c["role"] for c in eligible.values()}):
                raise ValueError("Completion requires at least two eligible verified contributors covering every discipline.")
            proposer_total = amount("0")
            for member, contribution in eligible.items():
                audit = Authority(d).authorize(actor, member, "quest_complete", q["shop_ids"],
                                               q["logical_id"], excluded=set(excluded) | {q["creator"]}, commit=True)
                completion = f"quest-complete:{member}:{q['logical_id']}"
                if not s.get("ledger_evidence", completion):
                    s.put("ledger_evidence", {"_id": completion, "kind": "quest_completion", "member_id": member,
                        "quest_revision": key, "logical_id": q["logical_id"], "description": contribution["description"],
                        "reviewer": actor, "review_authority": audit, "activity_at": contribution.get("submitted_at"), "at": now()})
                    from .avatars import request
                    request(d, member, "quest:" + completion)
                    before_xp = amount(d.participant(member)["xp"])
                    d.award(member, completion, str(contribution["reward"]), "quest", facts={
                        "quest_title": q["title"], "summary": "Independently verified shared quest completion."}, action_id=action_id)
                    credited = amount(d.participant(member)["xp"]) - before_xp
                    if member != q["creator"]:
                        proposer_total += credited
                    d.notify(member, "quest", {"quest_title": q["title"], "summary": "Independently verified shared quest completion.",
                             "verified_milestone": True, "milestone_id": completion, "xp_outcome_known": True}, "verified:" + completion, action_id=action_id)
                    d._advance(member, action_id=action_id)
            for member, contribution in state["contributions"].items():
                if member not in eligible:
                    contribution.update(status="closed", reason="Project completed; contribution was not verified and currently eligible. No XP awarded.")
                    d.notify(member, "quest", {"quest_title": q["title"], "summary": contribution["reason"]},
                             f"group-closed:{q['logical_id']}:{member}")
            state.update(status="completed", completed_at=now(), outcome=description, reviewer=actor,
                         review_authority=authority, awarded_members=sorted(eligible))
            s.put("ledger_relationships", state)
            q["interaction_revision"] = q.get("interaction_revision", 0) + 1
            s.put("ledger_quests", q)
            s.put("ledger_evidence", {"_id": "group-complete:" + q["logical_id"], "kind": "quest_group_completion",
                "quest_revision": key, "logical_id": q["logical_id"], "description": description,
                "actor": actor, **authority, "members": sorted(eligible), "at": now()})
            if not generated(q):
                from .quests import Quests
                Quests(d).award_proposer_royalty(q, "group-complete:" + q["logical_id"], proposer_total,
                                                 excluded=[])
            from .result_summaries import finish_action
            finish_action(d, action_id)
            return state
        return self.l.store.atomic(run)
