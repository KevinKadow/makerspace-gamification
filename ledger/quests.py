"""Reviewed member quests, with immutable revisions and one logical reward."""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from .authority import Authority
from .domain import Denied, Ledger, CHALLENGES
from .sources import object_id, sid
from .storage import now
from .quest_policy import (MEMBER_REVIEW_FIELDS, REVIEWED_KINDS, contains_rank_name, cooperative,
                           enabled_rank, generated, individual, minimum_rank, validate_duration,
                           validate_photo)
from .rules import amount


class Quests:
    def __init__(self, ledger):
        self.l = ledger

    def targets(self, author):
        p = self.l.require(author)
        if not self.l.sources.good_standing(author) or not self.l.sources.slack_id(author):
            raise Denied("Quest proposals require an active participant in good standing with a current Slack identity.")
        rules = self.l.store.get("ledger_rulesets", p["ruleset"])
        return [r["slot"] for r in rules["ranks"] if r["enabled"] and r["slot"] <= p["rank"]]

    def eligible_tools(self, author, search="", limit=100):
        """Return only live, visible tools covered by the author's current checkouts."""
        self.targets(author)
        search = search.casefold() if isinstance(search, str) else ""
        cleared = {sid(row.get("tool_id")) for row in self.l.sources.clearances(author)}
        rows = []
        for tool_id in sorted(cleared):
            tool = self.l.sources.tool(tool_id)
            shop = self.l.sources.shop((tool or {}).get("shop_id")) if tool else None
            if (not tool or tool.get("disabled") or tool.get("out_of_service") or not shop
                    or shop.get("disabled") or shop.get("out_of_service")):
                continue
            label = f"{tool.get('name', 'Tool')} — {shop.get('name', 'Shop')}"
            if search in label.casefold():
                rows.append({"value": tool_id, "label": label[:75], "shop_id": sid(tool["shop_id"])})
        return rows[:limit]

    def validate_author_tools(self, author, tools, shops=()):
        if not isinstance(tools, (list, tuple, set)) or len(tools) > 20:
            raise ValueError("Choose no more than 20 tools.")
        requested = {sid(value) for value in tools if value is not None}
        eligible = {row["value"]: row for row in self.eligible_tools(author, limit=1000)}
        if requested - eligible.keys():
            raise Denied("Choose only visible, available tools covered by your current checkout clearances.")
        derived = {eligible[value]["shop_id"] for value in requested}
        # Explicit shops remain accepted for legacy revisions and API callers,
        # but new Slack proposals derive their complete scope from tool choices.
        for shop_id in {sid(value) for value in shops if value is not None}:
            shop = self.l.sources.shop(shop_id)
            if not shop or shop.get("disabled") or shop.get("out_of_service"):
                raise ValueError("Choose available enabled shops.")
            derived.add(shop_id)
        return sorted(requested), sorted(derived)

    def explicit_shops(self, q):
        """Return shop-only prerequisites, including inferred legacy entries."""
        if isinstance(q.get("explicit_shop_ids"), list):
            return sorted({sid(value) for value in q["explicit_shop_ids"]})
        derived = set()
        for identifier in q.get("tool_ids", []):
            tool = self.l.sources.tool(identifier)
            if tool and tool.get("shop_id") is not None:
                derived.add(sid(tool["shop_id"]))
        return sorted({sid(value) for value in q.get("shop_ids", [])} - derived)

    def transition_project(self, reviewed):
        """Move the one active cooperative project to a reviewed logical revision."""
        project_id = "cooperative:" + reviewed["logical_id"]
        project = self.l.store.get("ledger_relationships", project_id)
        if not cooperative(reviewed):
            if (project and project.get("status") != "completed"
                    and project.get("quest_revision") != reviewed["_id"]):
                reason = "A reviewed individual revision replaced this cooperative project. No XP was awarded."
                project.update(status="superseded", superseded_by=reviewed["_id"], superseded_at=now(),
                               disable_reason=reason)
                for member, contribution in project.get("contributions", {}).items():
                    if contribution.get("status") != "closed":
                        contribution.update(status="closed", reason=reason)
                        self.l.notify(member, "quest", {"quest_title": reviewed["title"], "summary": reason},
                                      f"project-revision:{reviewed['logical_id']}:{project.get('quest_revision')}:{member}")
                self.l.store.put("ledger_relationships", project)
            return
        if not project:
            self.l.store.put("ledger_relationships", {"_id": project_id, "kind": "quest_project",
                "logical_id": reviewed["logical_id"], "quest_revision": reviewed["_id"],
                "status": "open", "contributions": {}, "at": now()})
            return
        if project.get("quest_revision") == reviewed["_id"] and project.get("status") == "open":
            return
        if project.get("status") == "completed":
            raise ValueError("A completed cooperative logical quest cannot be reopened by a later revision.")
        prior_revision = project.get("quest_revision")
        if not prior_revision:
            raise ValueError("The existing cooperative project has no quest revision.")
        archive_id = f"cooperative-revision:{reviewed['logical_id']}:{prior_revision}"
        if self.l.store.get("ledger_relationships", archive_id):
            raise ValueError("The prior cooperative project revision was already archived.")
        # Create the archive owner first so review-notice metadata on its
        # contributions is retained when the complete immutable snapshot lands.
        self.l.store.put("ledger_relationships", {"_id": archive_id, "kind": "quest_project",
            "logical_id": reviewed["logical_id"], "quest_revision": prior_revision,
            "status": "superseded", "contributions": {}})
        archived = deepcopy(project)
        archived.update(_id=archive_id, kind="quest_project", prior_status=project.get("status"),
                        status="superseded", superseded_by=reviewed["_id"], archived_at=now())
        reason = "A later reviewed quest revision replaced this cooperative project. No XP was awarded."
        archived["disable_reason"] = reason
        for member, contribution in archived.get("contributions", {}).items():
            if contribution.get("status") != "closed":
                contribution.update(status="closed", reason=reason)
                self.l.notify(member, "quest", {"quest_title": reviewed["title"], "summary": reason},
                              f"project-revision:{reviewed['logical_id']}:{prior_revision}:{member}")
        self.l.store.put("ledger_relationships", archived)
        self.l.store.put("ledger_relationships", {"_id": project_id, "kind": "quest_project",
            "logical_id": reviewed["logical_id"], "quest_revision": reviewed["_id"],
            "revision_of_project": prior_revision, "status": "open", "contributions": {}, "at": now()})

    def author_available(self, q):
        if generated(q):
            return enabled_rank(self.l, q["target_rank"]) and not contains_rank_name(self.l, q)
        p = self.l.participant(q["creator"])
        identity = self.l.store.get("ledger_catalog", f"identity:{q['creator']}") or {}
        if (not p or not self.l.sources.good_standing(q["creator"]) or not self.l.sources.permitted(q["creator"])
                or not self.l.sources.slack_id(q["creator"]) or identity.get("deactivated") or identity.get("bot")):
            return False
        rules = self.l.store.get("ledger_rulesets", p["ruleset"])
        author_limit = p["rank"] if minimum_rank(q) else p["rank"] - 2
        return bool(q["target_rank"] <= author_limit and any(r["slot"] == q["target_rank"] and r["enabled"] for r in rules["ranks"]))

    def prerequisites(self, q, member):
        for shop in q.get("shop_ids", []):
            doc = self.l.sources.shop(shop)
            if not doc or doc.get("disabled") or doc.get("out_of_service"):
                return False
        cleared = {sid(c["tool_id"]) for c in self.l.sources.rows("tool_checkouts", {"member_id": object_id(member), "revoked_at": None})}
        for tool in q.get("tool_ids", []):
            doc = self.l.sources.tool(tool)
            shop = self.l.sources.shop((doc or {}).get("shop_id")) if doc else None
            if (not doc or doc.get("disabled") or doc.get("out_of_service") or not shop
                    or shop.get("disabled") or shop.get("out_of_service") or tool not in cleared):
                return False
        return True

    def acceptance(self, member, logical):
        return self.l.store.get("ledger_relationships", f"acceptance:{member}:{logical}")

    def eligible(self, member, q, action="browse"):
        p = self.l.require(member)
        if (not individual(q) or q["status"] != "published" or not self.author_available(q)
                or (member == q["creator"] and not minimum_rank(q))):
            raise Denied("This quest is unavailable to you.")
        acceptance = self.acceptance(member, q["logical_id"])
        if acceptance:
            saved = self.l.store.get("ledger_quests", acceptance["quest_revision"])
            if not saved or saved["status"] != "published" or not self.author_available(saved):
                raise Denied("The accepted quest revision is unavailable.")
            q = saved
            if minimum_rank(q) and p["rank"] < q["target_rank"]:
                raise Denied("Your current rank is below this quest's approved minimum rank.")
        elif ((p["rank"] < q["target_rank"] if minimum_rank(q) else p["rank"] != q["target_rank"])
                or action in ("submit", "complete")):
            qualifier = "minimum rank" if minimum_rank(q) else "exact target rank"
            raise Denied(f"Meet the quest's {qualifier} and accept it first.")
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
                    {"kind": {"$in": list(REVIEWED_KINDS)}, "quest_type": "cooperative", "status": "published"}]})]:
            for row in self.l.store.select(collection, query):
                try:
                    self.detail(member, prefix + ":" + row["_id"])
                    if search.casefold() in row["title"].casefold():
                        rows.append((prefix + ":" + row["_id"], row["title"]))
                except Denied:
                    pass
        return sorted(rows, key=lambda r: (r[1].casefold(), r[0]))[:100]

    def draft(self, actor, title, description, criteria, target_rank, shops=(), tools=(), disciplines=(), revision_of=None,
              key=None, *, quest_type="individual", duration=None, photo=None):
        proposal_duration, proposal_photo, proposal_disciplines = duration, photo, disciplines
        def run(s):
            d = Ledger(s, self.l.sources)
            service = Quests(d)
            if type(target_rank) is not int or target_rank not in service.targets(actor):
                raise Denied("Choose an enabled minimum rank at or below your current rank.")
            if quest_type not in ("individual", "cooperative"):
                raise ValueError("Choose an individual or cooperative quest.")
            for value, limit in ((title, 100), (description, 2000), (criteria, 2000)):
                if not isinstance(value, str) or not value.strip() or len(value) > limit:
                    raise ValueError("Provide a title, description, and observable criteria within the form limits.")
            explicit_shop_ids = sorted({sid(value) for value in shops if value is not None})
            tool_ids, shop_ids = service.validate_author_tools(actor, tools, explicit_shop_ids)
            normalized_duration = validate_duration(proposal_duration or {"value": 1, "unit": "hours"})
            normalized_photo = validate_photo(proposal_photo)
            normalized_disciplines = list(proposal_disciplines or [])
            if quest_type == "individual" and normalized_disciplines:
                raise ValueError("Individual quests do not use cooperative disciplines.")
            if quest_type == "cooperative":
                if not 2 <= len(normalized_disciplines) <= 4:
                    raise ValueError("Cooperative quests require two to four disciplines.")
                names = set()
                for discipline in normalized_disciplines:
                    if (not isinstance(discipline, dict) or set(discipline) != {"name", "expectation"}
                            or not isinstance(discipline["name"], str) or not discipline["name"].strip()
                            or len(discipline["name"]) > 40 or not isinstance(discipline["expectation"], str)
                            or not discipline["expectation"].strip() or len(discipline["expectation"]) > 400):
                        raise ValueError("Each discipline needs a name and observable expectation.")
                    name = discipline["name"].strip().casefold()
                    if name in names:
                        raise ValueError("Cooperative disciplines must have distinct names.")
                    names.add(name)
                normalized_disciplines = [{"name": row["name"].strip(), "expectation": row["expectation"].strip()}
                                          for row in normalized_disciplines]
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
                   "quest_type": quest_type, "rank_mode": "minimum", "target_rank": target_rank,
                   "shop_ids": shop_ids, "explicit_shop_ids": explicit_shop_ids,
                   "tool_ids": tool_ids, "disciplines": normalized_disciplines,
                   "duration": normalized_duration, "photo": normalized_photo, "status": "draft", "at": now()}
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
            Quests(d).validate_author_tools(actor, q.get("tool_ids", []), q.get("shop_ids", []))
            if q["status"] == "pending_review":
                return q
            d.touch(actor)
            q.update(status="pending_review", submitted_at=now())
            s.put("ledger_quests", q)
            return q
        return self.l.store.atomic(run)

    def normalize_review(self, author, value):
        if not isinstance(value, dict) or set(value) != set(MEMBER_REVIEW_FIELDS):
            raise ValueError("Reviewed member quests require text, type, rank, tools, shops, disciplines, and duration.")
        result = deepcopy(value)
        for field, limit in (("title", 100), ("description", 2000), ("criteria", 2000)):
            if not isinstance(result[field], str) or not result[field].strip() or len(result[field]) > limit:
                raise ValueError("Provide a title, description, and observable criteria within the form limits.")
            result[field] = result[field].strip()
        if result["quest_type"] not in ("individual", "cooperative"):
            raise ValueError("Choose an individual or cooperative quest.")
        if type(result["target_rank"]) is not int or result["target_rank"] not in self.targets(author):
            raise Denied("The proposer is no longer eligible for that minimum rank.")
        result["tool_ids"], result["shop_ids"] = self.validate_author_tools(
            author, result["tool_ids"], result["shop_ids"])
        result["duration"] = validate_duration(result["duration"])
        disciplines = result["disciplines"]
        if result["quest_type"] == "individual":
            if disciplines:
                raise ValueError("Individual quests do not use cooperative disciplines.")
            result["disciplines"] = []
        else:
            if not isinstance(disciplines, list) or not 2 <= len(disciplines) <= 4:
                raise ValueError("Cooperative quests require two to four disciplines.")
            normalized = []
            for row in disciplines:
                if (not isinstance(row, dict) or set(row) != {"name", "expectation"}
                        or not isinstance(row["name"], str) or not row["name"].strip()
                        or len(row["name"]) > 40 or not isinstance(row["expectation"], str)
                        or not row["expectation"].strip() or len(row["expectation"]) > 400):
                    raise ValueError("Each discipline needs a name and observable expectation.")
                normalized.append({"name": row["name"].strip(), "expectation": row["expectation"].strip()})
            if len({row["name"].casefold() for row in normalized}) != len(normalized):
                raise ValueError("Cooperative disciplines must have distinct names.")
            result["disciplines"] = normalized
        return result

    def publish(self, actor, key, reward, classification="challenge", approve=True, reason="", catalog_id=None,
                proposer_bonus=100, edits=None):
        q = self.l.store.get("ledger_quests", key)
        if q and generated(q):
            if classification != "challenge" or catalog_id:
                raise ValueError("Ledger-generated quests use ordinary challenge rewards.")
            from .ledger_quests import LedgerQuests
            return LedgerQuests(self.l).review(actor, key, reward, approve, reason)
        def run(s):
            d = Ledger(s, self.l.sources)
            q = s.get("ledger_quests", key)
            receipt = s.get("ledger_evidence", "quest-review:" + key)
            if receipt and q and q.get("kind") == "member_quest":
                reviewed = s.get("ledger_quests", receipt.get("quest"))
                if not reviewed:
                    raise ValueError("The reviewed quest revision is unavailable.")
                Authority(d).authorize(actor, q["creator"], "quest_publish", q["shop_ids"], q["logical_id"])
                Authority(d).authorize(actor, q["creator"], "quest_publish", reviewed["shop_ids"], q["logical_id"])
                return reviewed
            if not q or q.get("kind") != "member_quest" or q["status"] != "pending_review":
                raise ValueError("Choose a pending quest revision.")
            service = Quests(d)
            Authority(d).authorize(actor, q["creator"], "quest_publish", q["shop_ids"], q["logical_id"])
            if type(reward) is not int or not 0 <= reward <= 500:
                raise ValueError("Quest rewards must be whole numbers from 0 to 500 XP.")
            if type(proposer_bonus) is not int or not 0 <= proposer_bonus <= 500:
                raise ValueError("Proposer approval bonuses must be whole numbers from 0 to 500 XP.")
            if classification not in CHALLENGES:
                raise ValueError("Select an existing milestone classification.")
            catalog = s.get("ledger_catalog", catalog_id) if catalog_id else None
            if classification != "challenge" and (not catalog or not catalog.get("active") or catalog.get("achievement") != classification):
                raise ValueError("Specialized milestones require an existing approved catalog entry and its evidence requirements.")
            if not approve and not reason.strip():
                raise ValueError("Rejection requires a reason.")
            original = {field: deepcopy(q.get(field)) for field in MEMBER_REVIEW_FIELDS}
            original["quest_type"] = original.get("quest_type") or "individual"
            original["duration"] = original.get("duration") or {"value": 1, "unit": "hours"}
            original["disciplines"] = original.get("disciplines") or []
            original["shop_ids"] = original.get("shop_ids") or []
            original["tool_ids"] = original.get("tool_ids") or []
            definition = service.normalize_review(q["creator"], edits if edits is not None else original) if approve else original
            prior_explicit = set(service.explicit_shops(q))
            inferred_explicit = set(service.explicit_shops(definition))
            reviewed_explicit = sorted(inferred_explicit | (prior_explicit & set(definition.get("shop_ids", []))))
            audit = Authority(d).authorize(actor, q["creator"], "quest_publish",
                                           definition.get("shop_ids", q["shop_ids"]), q["logical_id"], commit=True)
            reviewed = q
            if approve and (definition != original or q.get("rank_mode") != "minimum"
                            or any(field not in q for field in MEMBER_REVIEW_FIELDS)):
                reviewed = {**deepcopy(q), **definition, "rank_mode": "minimum", "_id": str(uuid4()), "revision_of": key,
                            "edited_by": actor, "edited_at": now()}
                q.update(status="superseded", superseded_by=reviewed["_id"])
                s.put("ledger_quests", q)
            reviewed.update(status="published" if approve else "rejected", reward=reward,
                            classification=classification, catalog_id=catalog_id, reviewer=actor,
                            reviewed_at=now(), review_authority=audit, reason=reason,
                            explicit_shop_ids=reviewed_explicit if approve else service.explicit_shops(q))
            s.put("ledger_quests", reviewed)
            if approve:
                from .quest_discovery import quest_head
                s.put("ledger_catalog", quest_head(reviewed))
                service.transition_project(reviewed)
            s.put("ledger_evidence", {"_id": "quest-review:" + key, "kind": "quest_review",
                "proposal": key, "quest": reviewed["_id"], "actor": actor, **audit, "at": now(),
                "reason": reason, "status": reviewed["status"]})
            if approve:
                bonus_key = "quest-proposer-approval:" + reviewed["logical_id"]
                if not s.get("ledger_evidence", bonus_key):
                    s.put("ledger_evidence", {"_id": bonus_key, "kind": "quest_proposer_approval",
                        "logical_id": reviewed["logical_id"], "proposer": reviewed["creator"],
                        "quest_revision": reviewed["_id"], "xp": str(proposer_bonus), "at": now()})
                    facts = {"quest_title": reviewed["title"],
                             "summary": f"Your quest was approved. Proposer approval bonus: {proposer_bonus} XP."}
                    if proposer_bonus:
                        d.award(reviewed["creator"], bonus_key, str(proposer_bonus), "quest", facts=facts)
                        d._advance(reviewed["creator"])
                    else:
                        d.notify(reviewed["creator"], "quest", facts, bonus_key)
            else:
                d.notify(reviewed["creator"], "quest", {"quest_title": reviewed["title"],
                         "summary": "Quest rejected after human review."}, "quest-review:" + key)
            return reviewed
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

    def award_proposer_royalty(self, q, completion_source, credited_xp, excluded=()):
        """Award one deterministic 5% proposer share from verified third-party XP."""
        proposer = q.get("creator")
        if (generated(q) or not proposer or proposer in set(excluded) or amount(credited_xp) <= 0
                or q.get("status") != "published" or not self.l.member_eligible(proposer)
                or not self.author_available(q) or not self.prerequisites(q, proposer)):
            return None
        value = (amount(credited_xp) * Decimal("0.05")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        if value <= 0:
            return None
        evidence_id = f"quest-royalty:{q['logical_id']}:{completion_source}"
        existing = self.l.store.get("ledger_evidence", evidence_id)
        if existing:
            return existing
        first = not self.l.store.select("ledger_evidence", {"kind": "quest_proposer_royalty",
            "logical_id": q["logical_id"], "proposer": proposer})
        evidence = {"_id": evidence_id, "kind": "quest_proposer_royalty",
            "logical_id": q["logical_id"], "proposer": proposer, "quest_revision": q["_id"],
            "completion_source": completion_source, "credited_xp": str(credited_xp),
            "xp": str(value), "first": first, "at": now()}
        self.l.store.put("ledger_evidence", evidence)
        summary = ("New Achievement! Your approved quest was completed for the first time. "
                   if first else "Another member completed your approved quest. ")
        summary += f"Proposer share: {value} XP."
        facts = {"quest_title": q["title"], "summary": summary}
        if not first:
            facts["_deterministic_text"] = "The System records another completion of your approved quest."
        self.l.award(proposer, evidence_id, str(value), "quest", facts=facts)
        self.l._advance(proposer)
        return evidence

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
                                              "logical_id": q["logical_id"], "submission_id": evidence, "at": now()})
                    d.touch(q["creator"])
                    q["completion_count"] = q.get("completion_count", 0) + 1
                    s.put("ledger_quests", q)
                    from .avatars import request
                    request(d, doc["member_id"], "quest:" + completion)
                    before_xp = amount(d.participant(doc["member_id"])["xp"])
                    d.award(doc["member_id"], completion, str(accepted["reward"]), "quest", facts={"quest_title": q["title"], "summary": "Independently verified quest completion."}, action_id=action_id)
                    credited = amount(d.participant(doc["member_id"])["xp"]) - before_xp
                    d.notify(doc["member_id"], "quest", {"quest_title": q["title"], "summary": "Independently verified quest completion.",
                             "verified_milestone": True, "milestone_id": completion, "xp_outcome_known": True}, "verified:" + completion, action_id=action_id)
                    d._advance(doc["member_id"], action_id=action_id)
                    if not generated(q):
                        Quests(d).award_proposer_royalty(q, completion, credited, excluded=[doc["member_id"]])
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
            if cooperative(q):
                project = s.get("ledger_relationships", "cooperative:" + q["logical_id"])
                if project and project.get("status") == "open" and project.get("quest_revision") == key:
                    reason = "The proposer withdrew this quest revision. No XP was awarded."
                    project.update(status="withdrawn", disable_reason=reason, withdrawn_at=now())
                    for member, contribution in project.get("contributions", {}).items():
                        if contribution.get("status") != "closed":
                            contribution.update(status="closed", reason=reason)
                            d.notify(member, "quest", {"quest_title": q["title"], "summary": reason},
                                     f"project-withdrawn:{q['logical_id']}:{key}:{member}")
                    s.put("ledger_relationships", project)
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
        capability = max(0, p["rank"])
        if capability and not p.get("quest_proposals_notified"):
            p["quest_proposals_notified"] = True
            p["quest_capability_notified"] = capability
            self.l.store.put("ledger_participants", p)
            self.l.notify(member, "quest", {"summary": f"You can propose individual or cooperative quests with enabled minimum ranks 1–{capability}. Use /ledger-quests create. Independent review is required."}, f"quest-unlock:{member}:{capability}", action_id=action_id)
