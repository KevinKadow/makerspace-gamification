"""Reviewed member quests, with immutable revisions and one logical reward."""
from copy import deepcopy
from uuid import uuid4

from .authority import Authority
from .domain import Denied, Ledger, CHALLENGES
from .sources import object_id, sid
from .storage import now
from .quest_policy import REVIEWED_KINDS, contains_rank_name, cooperative, enabled_rank, generated, individual


class Quests:
    def __init__(self, ledger):
        self.l = ledger

    def targets(self, author):
        p = self.l.require(author)
        if p["rank"] < 3 or not self.l.sources.good_standing(author):
            raise Denied("Quest authoring requires participation and rank slot 3 or higher.")
        rules = self.l.store.get("ledger_rulesets", p["ruleset"])
        return [r["slot"] for r in rules["ranks"] if r["enabled"] and r["slot"] <= p["rank"] - 2]

    def author_available(self, q):
        if generated(q):
            return enabled_rank(self.l, q["target_rank"]) and not contains_rank_name(self.l, q)
        p = self.l.participant(q["creator"])
        identity = self.l.store.get("ledger_catalog", f"identity:{q['creator']}") or {}
        if not p or not self.l.sources.permitted(q["creator"]) or identity.get("deactivated") or identity.get("bot"):
            return False
        rules = self.l.store.get("ledger_rulesets", p["ruleset"])
        return bool(q["target_rank"] <= p["rank"] - 2 and any(r["slot"] == q["target_rank"] and r["enabled"] for r in rules["ranks"]))

    def prerequisites(self, q, member):
        for shop in q.get("shop_ids", []):
            doc = self.l.sources.shop(shop)
            if not doc or doc.get("disabled") or (generated(q) and doc.get("out_of_service")):
                return False
        cleared = {sid(c["tool_id"]) for c in self.l.sources.rows("tool_checkouts", {"member_id": object_id(member), "revoked_at": None})}
        for tool in q.get("tool_ids", []):
            doc = self.l.sources.tool(tool)
            shop = self.l.sources.shop((doc or {}).get("shop_id")) if doc else None
            if not doc or doc.get("disabled") or not shop or shop.get("disabled") or tool not in cleared:
                return False
            if generated(q) and (doc.get("out_of_service") or shop.get("out_of_service")):
                return False
        return True

    def acceptance(self, member, logical):
        return self.l.store.get("ledger_relationships", f"acceptance:{member}:{logical}")

    def eligible(self, member, q, action="browse"):
        p = self.l.require(member)
        if not individual(q) or q["status"] != "published" or not self.author_available(q) or member == q["creator"]:
            raise Denied("This quest is unavailable to you.")
        acceptance = self.acceptance(member, q["logical_id"])
        if acceptance:
            saved = self.l.store.get("ledger_quests", acceptance["quest_revision"])
            if not saved or saved["status"] != "published" or not self.author_available(saved):
                raise Denied("The accepted quest revision is unavailable.")
            q = saved
        elif p["rank"] != q["target_rank"] or action in ("submit", "complete"):
            raise Denied("Accept this quest at its exact target rank first.")
        if not self.prerequisites(q, member):
            raise Denied("This quest's shop or tool prerequisites are not currently met.")
        if self.l.store.get("ledger_evidence", f"quest-complete:{member}:{q['logical_id']}"):
            raise Denied("You have already completed this quest.")
        return q

    def listing(self, member, search=""):
        from .read_options import optimized_reads
        if optimized_reads():
            from .quest_discovery import QuestDiscovery
            return QuestDiscovery(self.l, member).listing(search)
        rows = []
        for q in self.l.store.select("ledger_quests", {"kind": {"$in": list(REVIEWED_KINDS)}, "status": "published"}):
            if not individual(q):
                continue
            head = self.l.store.get("ledger_catalog", "quest-head:" + q["logical_id"])
            accepted = self.acceptance(member, q["logical_id"])
            if (not accepted and (not head or head["revision"] != q["_id"])) or (accepted and accepted["quest_revision"] != q["_id"]):
                continue
            try:
                current = self.eligible(member, q)
                if search.casefold() in current["title"].casefold():
                    rows.append(current)
            except Denied:
                continue
        return sorted(rows, key=lambda q: (q["title"].casefold(), q["_id"]))

    def detail(self, member, selected):
        self.l.require(member)
        prefix, key = selected.split(":", 1)
        if prefix == "q":
            q = self.l.store.get("ledger_quests", key)
            if not q:
                raise ValueError("Quest not found.")
            return self.eligible(member, q)
        if prefix == "c":
            catalog = self.l.store.get("ledger_catalog", key)
            if not catalog or catalog.get("kind") != "challenge" or not catalog.get("active"):
                raise Denied("This challenge is unavailable.")
            return catalog
        if prefix == "g":
            q = self.l.store.get("ledger_quests", key)
            if q and cooperative(q):
                from .ledger_quests import LedgerQuests
                return LedgerQuests(self.l).available(member, q)
            if not q or q.get("kind") in REVIEWED_KINDS or q["status"] != "open" or q["creator"] == member:
                raise Denied("This group quest is unavailable.")
            return q
        raise ValueError("Choose a published quest title.")

    def options(self, member, search=""):
        from .read_options import optimized_reads
        if optimized_reads():
            from .quest_discovery import QuestDiscovery
            return QuestDiscovery(self.l, member).options(search)
        rows = [("q:" + q["_id"], q["title"]) for q in self.listing(member, search)]
        for prefix, collection, query in [("c", "ledger_catalog", {"kind": "challenge", "active": True}),
                ("g", "ledger_quests", {"$or": [{"status": "open"},
                    {"kind": "ledger_quest", "quest_type": "cooperative", "status": "published"}]})]:
            for row in self.l.store.select(collection, query):
                try:
                    self.detail(member, prefix + ":" + row["_id"])
                    if search.casefold() in row["title"].casefold():
                        rows.append((prefix + ":" + row["_id"], row["title"]))
                except Denied:
                    pass
        return sorted(rows, key=lambda r: (r[1].casefold(), r[0]))[:100]

    def draft(self, actor, title, description, criteria, target_rank, shops=(), tools=(), disciplines=(), revision_of=None, key=None):
        def run(s):
            d = Ledger(s, self.l.sources)
            service = Quests(d)
            if type(target_rank) is not int or target_rank not in service.targets(actor):
                raise Denied("Choose an enabled rank at least two slots below your own.")
            for value, limit in ((title, 100), (description, 2000), (criteria, 2000)):
                if not isinstance(value, str) or not value.strip() or len(value) > limit:
                    raise ValueError("Provide a title, description, and observable criteria within the form limits.")
            shops_set = set(shops)
            for shop in shops_set:
                if not d.sources.shop(shop) or d.sources.shop(shop).get("disabled"):
                    raise ValueError("Choose enabled shops.")
            for tool in tools:
                doc = d.sources.tool(tool)
                if not doc or doc.get("disabled"):
                    raise ValueError("Choose enabled tools.")
                shop = d.sources.shop(doc.get("shop_id"))
                if not shop or shop.get("disabled"):
                    raise ValueError("Choose tools in enabled shops.")
                shops_set.add(sid(doc["shop_id"]))
            old = s.get("ledger_quests", revision_of) if revision_of else None
            if revision_of and (not old or old.get("kind") != "member_quest" or old["creator"] != actor):
                raise Denied("Only the creator may revise a member quest.")
            identifier = key or str(uuid4())
            previous = s.get("ledger_quests", identifier)
            if previous:
                if previous.get("creator") != actor or previous.get("kind") != "member_quest":
                    raise Denied("This draft belongs to another author.")
                return previous
            doc = {"_id": identifier, "kind": "member_quest", "logical_id": old["logical_id"] if old else identifier,
                   "creator": actor, "title": title, "description": description, "criteria": criteria,
                   "target_rank": target_rank, "shop_ids": sorted(shops_set), "tool_ids": sorted(set(tools)),
                   "disciplines": sorted(set(disciplines)), "status": "draft", "at": now()}
            d.touch(actor)
            s.put("ledger_quests", doc)
            return doc
        return self.l.store.atomic(run)

    def submit_draft(self, actor, key):
        def run(s):
            d = Ledger(s, self.l.sources)
            q = s.get("ledger_quests", key)
            if not q or q.get("kind") != "member_quest" or q["creator"] != actor or q["status"] not in ("draft", "pending_review"):
                raise Denied("Choose your unsubmitted draft.")
            if q["target_rank"] not in Quests(d).targets(actor):
                raise Denied("Your current rank cannot author this target.")
            if q["status"] == "pending_review":
                return q
            d.touch(actor)
            q.update(status="pending_review", submitted_at=now())
            s.put("ledger_quests", q)
            return q
        return self.l.store.atomic(run)

    def publish(self, actor, key, reward, classification="challenge", approve=True, reason="", catalog_id=None):
        q = self.l.store.get("ledger_quests", key)
        if q and generated(q):
            if classification != "challenge" or catalog_id:
                raise ValueError("Ledger-generated quests use ordinary challenge rewards.")
            from .ledger_quests import LedgerQuests
            return LedgerQuests(self.l).review(actor, key, reward, approve, reason)
        def run(s):
            d = Ledger(s, self.l.sources)
            q = s.get("ledger_quests", key)
            if not q or q.get("kind") != "member_quest" or q["status"] != "pending_review":
                raise ValueError("Choose a pending quest revision.")
            audit = Authority(d).authorize(actor, q["creator"], "quest_publish", q["shop_ids"], q["logical_id"], commit=True)
            if q["target_rank"] not in Quests(d).targets(q["creator"]):
                raise Denied("The author is no longer eligible to publish this quest.")
            if type(reward) is not int or not 0 <= reward <= 500:
                raise ValueError("Quest rewards must be whole numbers from 0 to 500 XP.")
            if classification not in CHALLENGES:
                raise ValueError("Select an existing milestone classification.")
            catalog = s.get("ledger_catalog", catalog_id) if catalog_id else None
            if classification != "challenge" and (not catalog or not catalog.get("active") or catalog.get("achievement") != classification):
                raise ValueError("Specialized milestones require an existing approved catalog entry and its evidence requirements.")
            if not approve and not reason.strip():
                raise ValueError("Rejection requires a reason.")
            q.update(status="published" if approve else "rejected", reward=reward, classification=classification,
                     catalog_id=catalog_id, reviewer=actor, reviewed_at=now(), review_authority=audit, reason=reason)
            s.put("ledger_quests", q)
            if approve:
                from .quest_discovery import quest_head
                s.put("ledger_catalog", quest_head(q))
            s.put("ledger_evidence", {"_id": "quest-review:" + key, "kind": "quest_review", "quest": key, "actor": actor, **audit, "at": now(), "reason": reason, "status": q["status"]})
            d.notify(q["creator"], "quest", {"quest_title": q["title"], "summary": "Quest " + q["status"] + ". Publication grants no XP."}, "quest-review:" + key)
            return q
        return self.l.store.atomic(run)

    def accept(self, member, key):
        def run(s):
            d = Ledger(s, self.l.sources)
            q = s.get("ledger_quests", key)
            if not q:
                raise ValueError("Quest not found.")
            service = Quests(d)
            q = service.eligible(member, q, "accept")
            previous = service.acceptance(member, q["logical_id"])
            if previous:
                return previous
            head = s.get("ledger_catalog", "quest-head:" + q["logical_id"])
            if not head or head["revision"] != key:
                raise Denied("This revision has been superseded. Browse quests again.")
            d.touch(member)
            d.touch(q["creator"])
            # Serialize acceptance with disable/withdraw of the revision.
            q["acceptance_count"] = q.get("acceptance_count", 0) + 1
            s.put("ledger_quests", q)
            doc = {"_id": f"acceptance:{member}:{q['logical_id']}", "kind": "quest_acceptance", "member_id": member,
                   "quest_revision": key, "logical_id": q["logical_id"], "rank": d.participant(member)["rank"], "reward": q["reward"], "at": now(),
                   "title": q["title"], "title_key": q["title"].casefold()}
            s.put("ledger_relationships", doc)
            return doc
        return self.l.store.atomic(run)

    def submit(self, member, key, description, learners=(), mentor=None, handoff=None):
        def run(s):
            d = Ledger(s, self.l.sources)
            q = s.get("ledger_quests", key)
            if not q:
                raise ValueError("Quest not found.")
            q = Quests(d).eligible(member, q, "submit")
            if not description.strip() or len(description) > 2000:
                raise ValueError("Describe observable completion evidence.")
            logical_submission = f"quest-submission:{member}:{q['logical_id']}"
            accepted = Quests(d).acceptance(member, q["logical_id"])
            # Old deployments stored one attempt under the logical ID. Keep it
            # intact; the acceptance becomes the pointer to subsequent attempts.
            previous_id = accepted.get("submission_id") or logical_submission
            previous = s.get("ledger_evidence", previous_id)
            if accepted.get("submission_id") and not previous:
                raise ValueError("The saved quest submission is unavailable.")
            if previous and previous["status"] in ("pending", "approved"):
                return previous
            submission_version = previous.get("submission_version", 1) + 1 if previous else 1
            submission_id = f"{logical_submission}:attempt:{submission_version}"
            if s.get("ledger_evidence", submission_id):
                raise ValueError("This quest submission version already exists.")
            specialized = None
            if q["classification"] != "challenge":
                specialized = d._submit(member, q["catalog_id"], description, list(learners), (q["shop_ids"] or [None])[0], mentor, handoff,
                                        f"member-quest:{q['logical_id']}:attempt:{submission_version}")
                specialized["quest_link"] = q["logical_id"]
                s.put("ledger_evidence", specialized)
            doc = {"_id": submission_id, "kind": "quest_submission", "member_id": member, "submission_version": submission_version,
                   "quest_revision": q["_id"], "logical_id": q["logical_id"], "description": description, "status": "pending", "at": now(),
                   "specialized_evidence": specialized["_id"] if specialized else None}
            d.touch(member)
            d.touch(q["creator"])
            s.put("ledger_evidence", doc)
            accepted["submission_id"] = submission_id
            s.put("ledger_relationships", accepted)
            return doc
        return self.l.store.atomic(run)

    def verify(self, actor, evidence, approve=True, reason="", *, action_id=None):
        action_id = action_id or "quest-review:" + str(uuid4())
        def run(s):
            d = Ledger(s, self.l.sources)
            doc = s.get("ledger_evidence", evidence)
            if not doc or doc.get("kind") != "quest_submission" or doc["status"] != "pending":
                raise ValueError("Choose a pending quest completion.")
            q = s.get("ledger_quests", doc["quest_revision"])
            review_id = f"review:quest-submission:{doc['member_id']}:{q['logical_id']}:attempt:{doc.get('submission_version', 1)}"
            if s.get("ledger_evidence", review_id):
                raise ValueError("This quest submission attempt was already reviewed.")
            audit = Authority(d).authorize(actor, doc["member_id"], "quest_complete", q["shop_ids"], q["logical_id"], excluded=[q["creator"]], commit=True)
            q = Quests(d).eligible(doc["member_id"], q, "complete")
            if not approve and not reason.strip():
                raise ValueError("Rejection requires a reason.")
            if approve and doc.get("specialized_evidence"):
                # Existing evidence gates still apply; reconciliation suppresses
                # the catalog XP for this quest-linked milestone.
                d._review(actor, doc["specialized_evidence"], True, reason, quest_review=True, action_id=action_id)
            doc.update(status="approved" if approve else "rejected", reviewer=actor, review_authority=audit, reason=reason, reviewed_at=now())
            s.put("ledger_evidence", doc)
            s.put("ledger_evidence", {"_id": review_id, "kind": "quest_completion_review", "evidence": evidence,
                "member_id": doc["member_id"], "logical_id": q["logical_id"], "quest_revision": q["_id"],
                "submission_version": doc.get("submission_version", 1), "status": doc["status"], "actor": actor, **audit, "at": now(), "reason": reason})
            if approve:
                completion = f"quest-complete:{doc['member_id']}:{q['logical_id']}"
                if not s.get("ledger_evidence", completion):
                    accepted = Quests(d).acceptance(doc["member_id"], q["logical_id"])
                    s.put("ledger_evidence", {"_id": completion, "kind": "quest_completion", "member_id": doc["member_id"], "quest_revision": q["_id"],
                                              "submission_id": evidence, "at": now()})
                    d.touch(q["creator"])
                    q["completion_count"] = q.get("completion_count", 0) + 1
                    s.put("ledger_quests", q)
                    d.award(doc["member_id"], completion, str(accepted["reward"]), "quest", facts={"quest_title": q["title"], "summary": "Independently verified quest completion."}, action_id=action_id)
                    d.notify(doc["member_id"], "quest", {"quest_title": q["title"], "summary": "Independently verified quest completion.",
                             "verified_milestone": True, "milestone_id": completion, "xp_outcome_known": True}, "verified:" + completion, action_id=action_id)
                    d._advance(doc["member_id"], action_id=action_id)
                    if not generated(q):
                        d.notify(q["creator"], "quest", {"quest_title": q["title"], "summary": "An independent reviewer verified a member's completion."}, completion + ":author")
            from .result_summaries import finish_action
            finish_action(d, action_id)
            return doc
        return self.l.store.atomic(run)

    def withdraw(self, actor, key):
        def run(s):
            d = Ledger(s, self.l.sources)
            d.require(actor)
            q = s.get("ledger_quests", key)
            if not q or q.get("kind") != "member_quest" or q["creator"] != actor:
                raise Denied("Only the author may withdraw this quest.")
            q.update(status="withdrawn", withdrawn_at=now())
            s.put("ledger_quests", q)
        return self.l.store.atomic(run)

    def disable(self, actor, key, reason):
        def run(s):
            d = Ledger(s, self.l.sources)
            q = s.get("ledger_quests", key)
            if not q or q.get("kind") not in REVIEWED_KINDS or not reason.strip():
                raise ValueError("Choose a reviewed quest and provide a reason.")
            if not Authority.covers(Authority(d).staff_scope(actor), q["shop_ids"]):
                raise Denied("Only staff with the quest's complete scope may disable it.")
            q.update(status="disabled", disabled_at=now(), disable_reason=reason)
            s.put("ledger_quests", q)
            if cooperative(q):
                project = s.get("ledger_relationships", "cooperative:" + q["logical_id"])
                if project and project["status"] == "open" and project["quest_revision"] == key:
                    project.update(status="disabled", disable_reason=reason)
                    s.put("ledger_relationships", project)
            s.put("ledger_evidence", {"_id": "quest-disable:" + str(uuid4()), "kind": "quest_disable", "quest": key, "actor": actor, "reason": reason, "at": now()})
            return q
        return self.l.store.atomic(run)

    def cleanup(self, author):
        for q in self.l.store.select("ledger_quests", {"kind": "member_quest", "creator": author}):
            if q["status"] in ("published", "pending_review", "draft") and not self.author_available(q):
                q.update(status="disabled", disabled_at=now(), disable_reason="Author eligibility or corrected rank lost; reviewed republication required.")
                self.l.store.put("ledger_quests", q)

    def unlock_notice(self, member, *, action_id=None):
        if not self.l.active(member):
            return
        p = self.l.participant(member)
        capability = max(0, p["rank"] - 2)
        if capability and capability > p.get("quest_capability_notified", 0):
            p["quest_capability_notified"] = capability
            self.l.store.put("ledger_participants", p)
            self.l.notify(member, "quest", {"summary": f"You can author quests for enabled rank slots 1–{capability}. Use /ledger-quests create. Independent publication review is required."}, f"quest-unlock:{member}:{capability}", action_id=action_id)
