"""Collaborative projects, scoped verification, and mutually accepted mentoring relationships."""
from uuid import uuid4
from .domain import Denied
from .storage import now, enqueue


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

    def quest(self, actor, quest_id, action, role=None, description=None, member=None):
        l = self.ledger
        def run(s):
            from .domain import Ledger
            d = Ledger(s, l.sources)
            if action != "verify":
                d.require(actor)
            q = s.get("ledger_quests", quest_id)
            if not q:
                raise ValueError("Quest not found.")
            if action == "join":
                if q["status"] != "open" or role not in q["roles"]:
                    raise ValueError("Choose an open quest and one of its disciplines.")
                q["contributions"].setdefault(actor, {"role": role, "status": "joined"})
            elif action == "submit":
                if q["status"] != "open" or actor not in q["contributions"] or not (description or "").strip():
                    raise ValueError("Join first, then describe your contribution.")
                q["contributions"][actor].update(description=description, status="pending")
            elif action == "verify":
                d.reviewer(actor, member, q.get("shop_id"))
                c = q["contributions"].get(member)
                if not c or c["status"] != "pending":
                    raise ValueError("No submitted contribution to verify.")
                c.update(status="verified", reviewer=actor)
                accepted = {m: c for m, c in q["contributions"].items() if c["status"] == "verified"}
                if len(accepted) >= 2 and set(q["roles"]).issubset({c["role"] for c in accepted.values()}):
                    q["status"] = "completed"
                    for m, contribution in accepted.items():
                        key = f"quest:{quest_id}:{m}"
                        if s.get("ledger_evidence", key):
                            continue
                        s.put("ledger_evidence", {"_id": key, "kind": "submission", "achievement": "boss", "catalog_id": f"quest:{quest_id}",
                              "member_id": m, "description": contribution["description"], "learners": [], "acknowledged": [],
                              "shop_id": q.get("shop_id"), "status": "approved", "reviewer": contribution["reviewer"], "at": now()})
                        d._reconcile(m)
            else:
                raise ValueError("Unknown quest action.")
            s.put("ledger_quests", q)
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
            enqueue(s, "ledger_outbox", f"project:{doc['_id']}:{len(doc['updates'])}", "project",
                    {"member_id": actor, "project_id": doc["_id"], "update": len(doc["updates"]) - 1})
            return doc
        return l.store.atomic(run)
