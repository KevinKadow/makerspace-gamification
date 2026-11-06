"""Collaborative projects, scoped verification, and mutually accepted mentoring relationships."""
from uuid import uuid4
from .domain import Denied, enqueue_home_refresh
from .sources import sid
from .storage import now, enqueue
from .rules import amount


def enqueue_project_home_refresh(ledger, project_id, update_number, phase):
    """Refresh active members' global project gallery after a visible change."""
    store = ledger.store
    participants = store.select("ledger_participants", {"opted_in": True})
    identities = ledger.sources.identities([p["_id"] for p in participants])
    for participant in participants:
        member_id = sid(participant["_id"])
        identity = identities.get(member_id)
        local_identity = store.get("ledger_catalog", f"identity:{member_id}") or {}
        if (identity and identity.get("status") not in ("suspended", "revoked")
                and not local_identity.get("deactivated") and not local_identity.get("bot")):
            enqueue_home_refresh(store, member_id,
                f"project:{project_id}:{update_number}:{phase}", identity["slack_id"])


class Community:
    def __init__(self, ledger):
        self.ledger = ledger

    def buddy(self, actor, target, action="offer"):
        l = self.ledger
        def run(s):
            from .domain import Ledger
            d = Ledger(s, l.sources)
            p = d.require(actor)
            key = f"buddy:{target}:{actor}" if action == "offer" else target
            if action == "offer":
                d.require(target)
                if actor == target or p["rank"] < 3:
                    raise Denied("Buddy offers require Initiate rank and another participating member.")
                doc = {"_id": key, "kind": "buddy", "mentor": actor, "learner": target, "status": "offered", "at": now()}
                s.put("ledger_relationships", doc)
                d.notify(target, "mentoring", {"summary": "A Success Buddy has offered support.", "buddy": key}, key)
            else:
                doc = s.get("ledger_relationships", key)
                if not doc or actor not in (doc["mentor"], doc["learner"]):
                    raise Denied("This is not your buddy relationship.")
                if action == "accept" and actor != doc["learner"]:
                    raise Denied("The learner must accept the offer.")
                if action == "accept" and (doc["status"] != "offered" or not d.active(doc["mentor"]) or d.participant(doc["mentor"])["rank"] < 3):
                    raise Denied("This buddy offer is no longer available.")
                doc["status"] = "active" if action == "accept" else "ended"
                s.put("ledger_relationships", doc)
            return doc
        return l.store.atomic(run)

    def create_quest(self, actor, title, criteria, roles, task_id, shop_id=None):
        l = self.ledger
        l.admin(actor)
        from .sources import object_id
        if not title.strip() or not criteria.strip() or len(set(roles)) < 2:
            raise ValueError("A quest needs a title, acceptance criteria, and two disciplines.")
        if not l.sources.rows("volunteer_tasks", {"_id": object_id(task_id)}):
            raise ValueError("Choose an existing volunteer opportunity.")
        doc = {"_id": str(uuid4()), "title": title, "criteria": criteria, "roles": sorted(set(roles)),
               "task_id": task_id, "shop_id": shop_id, "creator": actor, "status": "open", "contributions": {}, "at": now()}
        l.store.atomic(lambda s: s.put("ledger_quests", doc))
        return doc

    def quest(self, actor, quest_id, action, role=None, description=None, member=None, *, action_id=None):
        l = self.ledger
        action_id = action_id or "community-quest:" + str(uuid4())
        q = l.store.get("ledger_quests", quest_id)
        if q and q.get("kind") == "ledger_quest":
            from .ledger_quests import LedgerQuests
            return LedgerQuests(l).contribute(actor, quest_id, action, role, description, member)
        def run(s):
            from .domain import Ledger
            d = Ledger(s, l.sources)
            if action != "verify":
                d.require(actor)
            q = s.get("ledger_quests", quest_id)
            if not q:
                raise ValueError("Quest not found.")
            if q.get("kind") == "member_quest":
                raise ValueError("Use the member quest acceptance and completion actions.")
            if action == "verify" and actor in q["contributions"]:
                raise Denied("Contributors cannot verify their own group quest.")
            if action != "verify" and actor == q["creator"]:
                raise Denied("Authors cannot contribute to their own quests.")
            if action == "join":
                if q["status"] != "open" or role not in q["roles"]:
                    raise ValueError("Choose an open quest and one of its disciplines.")
                q["contributions"].setdefault(actor, {"role": role, "status": "joined"})
            elif action == "submit":
                if q["status"] != "open" or actor not in q["contributions"] or not (description or "").strip():
                    raise ValueError("Join first, then describe your contribution.")
                q["contributions"][actor].update(description=description, status="pending")
            elif action == "verify":
                authority = d.reviewer(actor, member, q.get("shop_id"), "quest_complete", quest=quest_id, commit=True)
                c = q["contributions"].get(member)
                if not c or c["status"] != "pending":
                    raise ValueError("No submitted contribution to verify.")
                c.update(status="verified", reviewer=actor, review_authority=authority)
                accepted = {m: c for m, c in q["contributions"].items() if c["status"] == "verified"}
                if len(accepted) >= 2 and set(q["roles"]).issubset({c["role"] for c in accepted.values()}):
                    q["status"] = "completed"
                    for m, contribution in accepted.items():
                        key = f"quest:{quest_id}:{m}"
                        if s.get("ledger_evidence", key):
                            continue
                        # Legacy verifications retain their reviewer without inventing grant metadata.
                        review_authority = contribution.get("review_authority") or {}
                        s.put("ledger_evidence", {"_id": key, "kind": "submission", "achievement": "boss", "catalog_id": f"quest:{quest_id}",
                              "member_id": m, "description": contribution["description"], "learners": [], "acknowledged": [],
                              "shop_id": q.get("shop_id"), "status": "approved", "reviewer": contribution["reviewer"], "at": now(), **review_authority})
                        account_key = f"account:{m}:challenge:quest:{quest_id}"
                        prior_account = s.get("ledger_evidence", account_key) or {}
                        d._reconcile(m, action_id=action_id)
                        facts = {"verified_milestone": True, "milestone_id": key, "xp_outcome_known": True,
                                 "quest_title": q["title"]}
                        account = s.get("ledger_evidence", account_key) or {}
                        if account.get("revision", 0) != prior_account.get("revision", 0):
                            award_id = f"{account_key}:{account['revision']}"
                            award = s.get("ledger_awards", award_id) or {}
                            if amount(award.get("delta", "0")) > 0:
                                facts.update(award_id=award_id, xp_change=award["delta"], xp_total=d.participant(m)["xp"])
                        d.notify(m, "boss", facts, "verified:" + key, action_id=action_id)
            else:
                raise ValueError("Unknown quest action.")
            s.put("ledger_quests", q)
            from .result_summaries import finish_action
            finish_action(d, action_id)
            return q
        return l.store.atomic(run)

    def project(self, actor, title, description, collaborators=None, project_id=None):
        l = self.ledger
        def run(s):
            from .domain import Ledger
            d = Ledger(s, l.sources)
            d.require(actor)
            if not title.strip() or not description.strip():
                raise ValueError("A project needs a title and update.")
            doc = s.get("ledger_projects", project_id) if project_id else None
            if project_id and (not doc or doc["owner"] != actor):
                raise Denied("Only the owner may update a showcase.")
            if not doc:
                doc = {"_id": str(uuid4()), "owner": actor, "updates": [], "at": now()}
            people = list(set(collaborators or []))
            if any(not d.active(m) for m in people):
                raise ValueError("Collaborator credits must reference participating members.")
            doc.update(title=title, collaborators=people)
            doc["updates"].append({"description": description, "at": now()})
            s.put("ledger_projects", doc)
            enqueue_project_home_refresh(d, doc["_id"], len(doc["updates"]), "recorded")
            enqueue(s, "ledger_outbox", f"project:{doc['_id']}:{len(doc['updates'])}", "project",
                    {"member_id": actor, "project_id": doc["_id"], "update": len(doc["updates"]) - 1})
            return doc
        return l.store.atomic(run)
