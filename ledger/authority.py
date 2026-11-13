"""Application-issued review authority. All mutations run inside Ledger transactions."""
from uuid import uuid4

from .domain import Denied
from .sources import sid
from .storage import now

CAPABILITIES = {"quest_publish", "quest_complete", "learning_review", "mentoring_review"}


def evidence_capability(doc):
    return "mentoring_review" if doc.get("achievement") in ("mentoring", "develop_mentor") else "learning_review"


class Authority:
    def __init__(self, ledger):
        self.l = ledger

    def staff_scope(self, actor):
        identity = self.l.store.get("ledger_catalog", f"identity:{actor}") or {}
        if not self.l.sources.permitted(actor) or identity.get("deactivated") or identity.get("bot"):
            return None
        member = self.l.sources.member(actor) or {}
        if member.get("role") in ("admin", "board_member"):
            return {"kind": "global"}
        if member.get("role") == "resource_manager":
            return {"kind": "shops", "shops": sorted({sid(i) for i in member.get("resource_manager_shop_ids", [])})}
        return None

    @staticmethod
    def covers(scope, shops=(), quest=None):
        shops = set(shops) - {None, ""}
        return bool(scope and (scope["kind"] == "global" or
                    (scope["kind"] == "quest" and quest and scope["quest"] == quest) or
                    (scope["kind"] == "shops" and shops and shops.issubset(scope["shops"]))))

    def grant_valid(self, grant):
        p = self.l.participant(grant["delegate"])
        if (grant["status"] != "active" or not p or not self.l.active(grant["delegate"])
                or not self.l.sources.good_standing(grant["delegate"])
                or grant["consent_generation"] != p.get("consent_generation", 0)):
            return False
        scope = grant["scope"]
        staff = self.staff_scope(grant["grantor"])
        if scope["kind"] == "global":
            return bool(staff and staff["kind"] == "global")
        shops = scope.get("shops", [])
        if scope["kind"] == "quest":
            quest = self.l.store.get("ledger_quests", grant.get("quest_revision") or scope["quest"])
            if not quest:
                return False
            shops = quest.get("shop_ids") or [quest.get("shop_id")]
        return self.covers(staff, shops)

    def authorize(self, actor, subject, capability, shops=(), quest=None, excluded=(), commit=False):
        if capability not in CAPABILITIES or actor == subject or actor in excluded:
            raise Denied("Independent review is required.")
        if self.covers(self.staff_scope(actor), shops):
            if commit:
                self.l.touch(actor)
            return {"authority": "staff", "grant_id": None, "grant_version": None}
        for grant in self.l.store.select("ledger_relationships", {"kind": "delegation", "delegate": actor, "status": "active"}):
            scope = grant["scope"]
            if scope["kind"] == "quest":
                selected = self.l.store.get("ledger_quests", grant.get("quest_revision") or scope["quest"])
                if not selected:
                    continue
                # Resolve older revision-scoped grants without rewriting their audit history.
                scope = {"kind": "quest", "quest": selected.get("logical_id") or selected["_id"]}
            if (capability in grant["capabilities"] and self.grant_valid(grant) and self.covers(scope, shops, quest)
                    and (grant["scope"]["kind"] != "quest" or self.covers(self.staff_scope(grant["grantor"]), shops))):
                if commit:
                    # Explicit revocation writes this same document, forcing a
                    # Mongo transaction conflict rather than stale-modal approval.
                    grant["uses"] = grant.get("uses", 0) + 1
                    self.l.store.put("ledger_relationships", grant)
                    self.l.touch(actor)
                    self.l.touch(subject)
                return {"authority": "delegated", "grant_id": grant["_id"], "grant_version": grant["version"]}
        raise Denied("No current review grant covers this capability and scope.")

    def grant(self, actor, delegate, capabilities, scope, reason):
        requested_scope = scope
        def run(s):
            from .domain import Ledger
            d = Ledger(s, self.l.sources)
            a = Authority(d)
            scope = requested_scope
            selected = None
            d.require(delegate)
            if actor == delegate or not d.sources.good_standing(delegate) or not reason.strip():
                raise Denied("Select another participating member and provide a reason.")
            if not capabilities or set(capabilities) - CAPABILITIES:
                raise ValueError("Select explicit supported capabilities.")
            if not isinstance(scope, dict) or scope.get("kind") not in ("global", "shops", "quest"):
                raise ValueError("Choose global, shops, or quest scope.")
            if scope["kind"] == "shops":
                if set(scope) != {"kind", "shops"} or not scope["shops"] or any(not d.sources.shop(i) for i in scope["shops"]):
                    raise ValueError("Select existing shops.")
                scope = {"kind": "shops", "shops": sorted(set(scope["shops"]))}
            elif scope["kind"] == "quest":
                if set(scope) != {"kind", "quest"}:
                    raise ValueError("Select an existing quest.")
                selected = s.get("ledger_quests", scope["quest"])
                if not selected:
                    raise ValueError("Select an existing quest.")
                if not self.covers(a.staff_scope(actor), selected.get("shop_ids") or [selected.get("shop_id")]):
                    raise Denied("A grant cannot exceed the grantor's current staff authority.")
                scope = {"kind": "quest", "quest": selected.get("logical_id") or selected["_id"]}
            elif scope["kind"] == "global" and set(scope) != {"kind"}:
                raise ValueError("Invalid global scope.")
            doc = {"_id": "grant:" + str(uuid4()), "kind": "delegation", "delegate": delegate,
                   "grantor": actor, "capabilities": sorted(set(capabilities)), "scope": scope,
                   "consent_generation": d.participant(delegate).get("consent_generation", 0),
                   "status": "active", "version": 1, "at": now(), "reason": reason}
            if selected:
                # Retain the selected revision's shops for grantor eligibility checks.
                doc["quest_revision"] = selected["_id"]
            if not a.grant_valid(doc):
                raise Denied("A grant cannot exceed the grantor's current staff authority.")
            d.touch(delegate)
            d.touch(actor)
            s.put("ledger_relationships", doc)
            a.audit(doc, actor, "granted", reason)
            d.notify(delegate, "status", {"summary": "Review authority granted: " + ", ".join(doc["capabilities"]), "scope": scope}, doc["_id"])
            return doc
        return self.l.store.atomic(run)

    def audit(self, grant, actor, action, reason):
        self.l.store.put("ledger_evidence", {"_id": f"{grant['_id']}:{grant['version']}:{action}", "kind": "delegation_audit",
            "grant_id": grant["_id"], "grant_version": grant["version"], "actor": actor, "action": action, "reason": reason, "at": now()})

    def revoke_doc(self, grant, actor, reason):
        if grant["status"] != "active":
            return grant
        grant.update(status="revoked", version=grant["version"] + 1, revoked_at=now(), revocation_reason=reason)
        self.l.store.put("ledger_relationships", grant)
        self.audit(grant, actor, "revoked", reason)
        self.l.notify(grant["delegate"], "status", {"summary": "Review authority revoked: " + reason}, f"{grant['_id']}:{grant['version']}")
        return grant

    def revoke(self, actor, key, reason):
        def run(s):
            from .domain import Ledger
            d = Ledger(s, self.l.sources)
            grant = s.get("ledger_relationships", key)
            if not grant or grant.get("kind") != "delegation" or not reason.strip():
                raise ValueError("Choose a grant and provide a revocation reason.")
            a = Authority(d)
            staff = a.staff_scope(actor)
            if not staff or (staff["kind"] != "global" and actor != grant["grantor"]):
                raise Denied("Only authorized staff may revoke this grant.")
            return a.revoke_doc(grant, actor, reason)
        return self.l.store.atomic(run)

    def cleanup(self, member=None, reason="Current eligibility or grantor scope lost"):
        for grant in self.l.store.select("ledger_relationships", {"kind": "delegation", "status": "active"}):
            if (member is None or member in (grant["delegate"], grant["grantor"])) and not self.grant_valid(grant):
                self.revoke_doc(grant, "application", reason)
