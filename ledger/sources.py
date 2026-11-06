"""Read-only projections over the Rails-owned Mongo collections."""
from bson import ObjectId
from .storage import matches, now

FIELDS = {
    "members": "firstname lastname status expirationTime subscription subscription_id groupName merged_at role resource_manager_shop_ids",
    "slack_users": "member_id slack_id invalidated_at",
    "shops": "name disabled",
    "tools": "name shop_id prerequisite_ids disabled open out_of_service",
    "tool_checkouts": "member_id tool_id approved_by_id checked_out_at revoked_at volunteer_credit_id",
    "checkout_approvers": "member_id shop_ids tool_ids",
    "volunteer_credits": "member_id credit_value status tool_checkout_id task_id created_at reversed reversal_of_id",
    "volunteer_tasks": "title description shop_id status prerequisite_tool_ids claimed_by_id verified_by_id completed_at",
    "volunteer_events": "title shop_id status event_date attendee_ids",
    "earned_memberships": "member_id status",
    "groups": "groupName subscription subscription_id expiry",
}


def object_id(value):
    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    return value


def sid(value):
    return str(value) if value is not None else None


class Sources:
    def __init__(self, database):
        self.db = database

    def ready(self):
        self.db.command("ping")

    def rows(self, name, query=None):
        if name not in FIELDS:
            raise ValueError("Source collection is not allowlisted")
        projection = dict.fromkeys(FIELDS[name].split(), 1)
        return list(self.db[name].find(query or {}, projection))

    def member(self, member_id):
        rows = self.rows("members", {"_id": object_id(member_id), "merged_at": None})
        return rows[0] if rows else None

    def identity(self, slack_id):
        rows = self.rows("slack_users", {"slack_id": slack_id, "invalidated_at": None})
        if len(rows) != 1 or not rows[0].get("member_id"):
            return None
        member_id = rows[0]["member_id"]
        return self.member(member_id) if self.slack_id(member_id) == slack_id else None

    def slack_id(self, member_id):
        rows = self.rows("slack_users", {"member_id": object_id(member_id), "invalidated_at": None})
        uid = rows[0].get("slack_id") if len(rows) == 1 else None
        return uid if uid and len(self.rows("slack_users", {"slack_id": uid, "invalidated_at": None})) == 1 else None

    def good_standing(self, member_id):
        m = self.member(member_id)
        return bool(m and m.get("status") in ("activeMember", "pending") and self.slack_id(member_id))

    def permitted(self, member_id):
        m = self.member(member_id)
        return bool(m and m.get("status") not in ("suspended", "revoked") and self.slack_id(member_id))

    def role(self, member_id):
        return (self.member(member_id) or {}).get("role", "member")

    def eligible_for_rank(self, member_id, attestation=None):
        m = self.member(member_id)
        if not m or m.get("status") not in ("activeMember", "pending"):
            return False
        expiry = m.get("expirationTime") or 0
        if expiry <= int(now().timestamp() * 1000):
            return False
        earned = self.rows("earned_memberships", {"member_id": m["_id"], "status": "active"})
        groups = self.rows("groups", {"groupName": m.get("groupName")}) if m.get("groupName") else []
        paid = m.get("subscription") or m.get("subscription_id") or any(
            g.get("subscription") or g.get("subscription_id") for g in groups)
        verified_prepaid = attestation and attestation.get("expiration") == expiry and attestation.get("approved")
        return bool(paid or earned or verified_prepaid)

    def tool(self, tool_id):
        rows = self.rows("tools", {"_id": object_id(tool_id)})
        return rows[0] if rows else None

    def shop(self, shop_id):
        rows = self.rows("shops", {"_id": object_id(shop_id)})
        return rows[0] if rows else None


class MemorySources(Sources):
    def __init__(self, data):
        self.data = data

    def rows(self, name, query=None):
        allowed = set(FIELDS[name].split()) | {"_id"}
        return [{k: v for k, v in r.items() if k in allowed} for r in self.data.get(name, []) if matches(r, query or {})]
