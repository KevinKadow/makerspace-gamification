"""Hard projection boundary independent of Rails discovery and remote prompts."""
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import math
import re

UTC = timezone.utc
MEMBER_STATUSES = {"activeMember", "pending", "nonMember", "revoked", "inactive", "suspended"}
EVENT_TYPES = {"membership_revoked", "subscription_cancelled", "subscription_auto_cancelled_payment_failure",
               "invoice_settled", "member_updated", "membership_renewed", "invoice_payment_failed"}
ALLOWED = {
    "members": {"status", "expirationTime", "startDate", "subscription", "groupName", "address_city", "address_state", "address_postal_code"},
    "groups": {"groupName", "expiry", "subscription"},
    "cards": {"member_id", "uid"}, "checkins": {"uid", "timeOf", "time"},
    "invoices": {"member_id", "resource_id", "resource_class", "settled_at", "settlement_processed_at", "created_at", "quantity",
                 "operation", "last_failed_transaction_id", "refunded"},
    "audit_logs": {"subject_id", "resource_id", "resource_type", "event_type", "created_at", "field_changes"},
    "earned_memberships": {"member_id", "active", "created_at"},
}


def timestamp(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if type(value) in (int, float) and math.isfinite(value):
        seconds = value / 1000 if abs(value) >= 100_000_000_000 else value
        try:
            result = datetime.fromtimestamp(seconds, UTC)
            return result if 1970 <= result.year <= 2200 else None
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str) and len(value) < 80:
        try:
            return timestamp(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def identifier(value):
    from bson import ObjectId
    if isinstance(value, ObjectId):
        return str(value)
    if type(value) is int:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        return value
    return None


def digest(value, key):
    return hmac.new(key.encode(), str(value).encode(), sha256).hexdigest()


def normalize(collection, document, secret):
    """Return only approved normalized fields. No raw billing amounts or credentials."""
    if not isinstance(document, dict):
        return {}
    result = {}
    if collection == "invoices":
        amount = document.get("amount")
        result["paid_positive"] = type(amount) in (int, float) and math.isfinite(amount) and amount > 0 and bool(document.get("transaction_id"))
    if collection == "audit_logs":
        snapshot = document.get("after_snapshot")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("resource_class"), str) and snapshot["resource_class"] in {"member", "household", "rental", "fee"}:
            result["resource_class"] = snapshot["resource_class"]
            if identifier(snapshot.get("resource_id")):
                result["billing_resource_id"] = identifier(snapshot["resource_id"])
    for name in ALLOWED.get(collection, set()):
        value = document.get(name)
        if value is None:
            continue
        if name == "uid":
            if isinstance(value, (str, int)):
                result["uid_hash"] = digest(value, secret)
        elif name == "last_failed_transaction_id":
            result["payment_failed"] = bool(value)
        elif name in {"member_id", "subject_id", "resource_id", "groupName"}:
            if identifier(value):
                result[name] = identifier(value)
        elif name in {"expirationTime", "startDate", "expiry", "timeOf", "time", "created_at", "settled_at", "settlement_processed_at"}:
            if timestamp(value):
                result[name] = timestamp(value).isoformat()
        elif name == "field_changes" and isinstance(value, dict):
            safe = {}
            for field in ("status", "expirationTime", "expiry", "subscription"):
                pair = value.get(field)
                if not isinstance(pair, list) or len(pair) != 2:
                    continue
                if field == "status" and all(v is None or isinstance(v, str) and v in MEMBER_STATUSES for v in pair):
                    safe[field] = pair
                elif field == "subscription" and all(v is None or type(v) is bool for v in pair):
                    safe[field] = pair
                elif field in {"expirationTime", "expiry"} and all(v is None or timestamp(v) for v in pair):
                    safe[field] = [timestamp(v).isoformat() if v is not None else None for v in pair]
            result[name] = safe
        elif name == "event_type":
            if isinstance(value, str) and value in EVENT_TYPES:
                result[name] = value
        elif name == "status":
            if isinstance(value, str) and value in MEMBER_STATUSES:
                result[name] = value
        elif name in {"subscription", "active", "refunded"} and type(value) is bool:
            result[name] = value
        elif name == "quantity" and type(value) is int and 0 < value <= 120:
            result[name] = value
        elif name == "resource_class" and isinstance(value, str) and value in {"member", "household", "rental", "fee"}:
            result[name] = value
        elif name == "resource_type" and isinstance(value, str) and value in {"Member", "Group", "Invoice"}:
            result[name] = value
        elif name == "operation" and value == "renew=":
            result[name] = value
        elif name.startswith("address_") and isinstance(value, str) and re.fullmatch(r"[\w .'-]{1,64}", value):
            result[name] = value.strip().casefold()
    return result


def aggregate_rows(rows, minimum=5):
    """Suppress the complete dimension when any bucket is small, preventing subtraction."""
    if minimum < 5:
        raise ValueError("Minimum cohort cannot be less than five")
    groups = {}
    for row in rows:
        groups.setdefault((row["report"], row["dimension"]), []).append(row)
    return [r for group in groups.values() if all(r["sample_size"] >= minimum for r in group) for r in group]
