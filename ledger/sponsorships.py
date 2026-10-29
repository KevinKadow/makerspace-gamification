"""Private sponsor invitation history and authoritative Slack rendering."""
from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo

from .sources import sid
from .storage import now


SPONSORSHIPS_TOOL = {"type": "function", "function": {
    "name": "my_sponsorships",
    "description": ("Read only participation invitations sent by the caller. Use summary for counts, list for the "
                    "complete register, and detail for one person. Available only in a private DM."),
    "parameters": {"type": "object", "properties": {
        "mode": {"type": "string", "enum": ["summary", "list", "detail"]},
        "invitee": {"type": "string", "maxLength": 100},
    }, "required": ["mode"], "additionalProperties": False},
}}

DISPLAY_ZONE = ZoneInfo("America/New_York")
TABLE_ROW_LIMIT = 100
TABLE_CHAR_LIMIT = 9500
SPONSORSHIP_OPENERS = frozenset({
    "The Ledger opens your private sponsorship register.",
    "Here is your private sponsorship register.",
    "Your private sponsorship register is ready.",
})


def invitation_key(giver, recipient):
    return f"sponsor-invite:{giver}:{recipient}"


def backfill(store):
    """Copy legacy canonical sponsor rows into the per-inviter history."""
    count = 0
    for relationship in store.select("ledger_relationships", {"kind": "sponsor"}):
        giver, recipient = relationship.get("giver"), relationship.get("recipient")
        if not isinstance(giver, str) or not isinstance(recipient, str):
            continue
        key = invitation_key(giver, recipient)
        if store.get("ledger_relationships", key):
            continue
        store.put("ledger_relationships", {"_id": key, "kind": "sponsor_invitation",
            "giver": giver, "recipient": recipient, "at": relationship.get("at") or now(),
            "source": "sponsor_command", "legacy_backfill": True})
        count += 1
    return count


def caller_invitation(ledger, giver, recipient):
    row = ledger.store.get("ledger_relationships", invitation_key(giver, recipient))
    if row and row.get("kind") == "sponsor_invitation" and row.get("giver") == giver and row.get("recipient") == recipient:
        return row
    legacy = ledger.store.get("ledger_relationships", f"sponsor:{recipient}")
    return legacy if legacy and legacy.get("kind") == "sponsor" and legacy.get("giver") == giver else None


def _invitations(ledger, giver):
    records = {row["recipient"]: row for row in ledger.store.select("ledger_relationships", {
        "kind": "sponsor_invitation", "giver": giver})
        if isinstance(row.get("recipient"), str)}
    for row in ledger.store.select("ledger_relationships", {"kind": "sponsor", "giver": giver}):
        recipient = row.get("recipient")
        if isinstance(recipient, str) and recipient not in records:
            records[recipient] = {"_id": invitation_key(giver, recipient), "kind": "sponsor_invitation",
                "giver": giver, "recipient": recipient, "at": row.get("at"),
                "source": "sponsor_command", "legacy_backfill": True}
    return list(records.values())


def _stamp(value):
    if not isinstance(value, datetime):
        return float("-inf")
    return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()


def _date(value):
    if not isinstance(value, datetime):
        return "—"
    value = value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(DISPLAY_ZONE)
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def _safe_name(member):
    name = " ".join(str(member.get(key) or "").strip() for key in ("firstname", "lastname")).strip()
    name = re.sub(r"[\x00-\x1f\x7f]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()[:160] or "Member record unavailable"


def _normalize_name(value):
    return " ".join(re.sub(r"[^\w ]", "", str(value)).casefold().split())


def build_report(ledger, giver, mode="list", recipient=None, invitee=None):
    """Build a caller-owned immutable display snapshot; never exposes non-invitees."""
    if mode not in ("summary", "list", "detail"):
        raise ValueError("Unknown sponsorship report mode")
    invitations = _invitations(ledger, giver)
    recipient_ids = [row["recipient"] for row in invitations]
    members = ledger.sources.members_by_id(recipient_ids, "firstname lastname") if recipient_ids else {}

    selected = invitations
    status = "ok"
    if mode == "detail":
        if recipient is not None:
            selected = [row for row in invitations if row["recipient"] == recipient]
        else:
            query = str(invitee or "").strip()
            mention = re.fullmatch(r"<@([UW][A-Z0-9]+)(?:\|[^>]+)?>", query)
            if mention:
                identity = ledger.sources.identity(mention.group(1))
                target = sid(identity["_id"]) if identity else None
                selected = [row for row in invitations if row["recipient"] == target]
            else:
                wanted = _normalize_name(query)
                exact, first = [], []
                for row in invitations:
                    member = members.get(row["recipient"], {})
                    full = _normalize_name(" ".join(str(member.get(k) or "") for k in ("firstname", "lastname")))
                    given = _normalize_name(member.get("firstname", ""))
                    if wanted and full == wanted:
                        exact.append(row)
                    elif wanted and given == wanted:
                        first.append(row)
                selected = exact or first
            if not selected:
                status = "not_found"
            elif len(selected) > 1:
                status = "ambiguous"

    ids = [row["recipient"] for row in selected]
    participants = {row.get("member_id", row.get("_id")): row for row in ledger.store.select("ledger_participants", {
        "member_id": {"$in": ids}})} if ids else {}
    consent = ledger.store.select("ledger_evidence", {"kind": "consent", "member_id": {"$in": ids}}) if ids else []
    latest_in, latest_out = {}, {}
    for event in consent:
        target = event.get("member_id")
        bucket = latest_in if event.get("opted_in") is True else latest_out if event.get("opted_in") is False else None
        if bucket is not None and _stamp(event.get("at")) > _stamp(bucket.get(target)):
            bucket[target] = event.get("at")

    rows = []
    for invitation in selected:
        target = invitation["recipient"]
        participant = participants.get(target)
        if participant and participant.get("opted_in") is True:
            state = "Opted in"
        elif participant:
            state = "Opted out"
        else:
            state = "Never opted in"
        rows.append({"recipient": target, "name": _safe_name(members.get(target, {})),
            "invited": _date(invitation.get("at")), "status": state,
            "latest_opt_in": _date(latest_in.get(target) or (participant or {}).get("first_opt_in")),
            "latest_opt_out": _date(latest_out.get(target)), "sort_at": _stamp(invitation.get("at"))})
    rows.sort(key=lambda row: (-row["sort_at"], row["name"].casefold(), row["recipient"]))
    for row in rows:
        row.pop("sort_at", None)
    totals = {label: sum(row["status"] == label for row in rows)
              for label in ("Opted in", "Opted out", "Never opted in")}
    return {"mode": mode, "status": status, "as_of": now().isoformat(), "rows": rows,
            "totals": {"Invited": len(rows), **totals}}


def tool_result(report):
    """Return only routing metadata to Qwen; Python retains and renders all facts."""
    return {"status": report["status"], "mode": report["mode"],
            "result": "The application will append the authoritative private sponsorship report."}


def valid_opener(text, report=None):
    """Accept only reviewed, fact-free phrases; generated prose cannot claim outcomes."""
    return isinstance(text, str) and text.strip() in SPONSORSHIP_OPENERS


def _cell(text):
    return {"type": "raw_text", "text": str(text)}


def _fallback_cell(text):
    """Escape Slack control syntax in top-level accessibility text."""
    value = re.sub(r"[\r\n]+", " ", str(text))
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _table_pages(header, data):
    pages, rows, characters = [], [[_cell(value) for value in header]], sum(map(len, header))
    for values in data:
        size = sum(len(str(value)) for value in values)
        if len(rows) >= TABLE_ROW_LIMIT or characters + size > TABLE_CHAR_LIMIT:
            pages.append(rows)
            rows, characters = [[_cell(value) for value in header]], sum(map(len, header))
        rows.append([_cell(value) for value in values])
        characters += size
    if len(rows) > 1:
        pages.append(rows)
    return pages


def render_report(report, opener="The Ledger opens your private sponsorship register."):
    """Return complete, limit-aware Slack messages with deterministic facts."""
    if report["mode"] == "summary":
        data = [[label, value] for label, value in report["totals"].items()]
        tables = _table_pages(["Status", "Count"], data)
    else:
        data = [[row["name"], row["invited"], row["status"], row["latest_opt_in"], row["latest_opt_out"]]
                for row in report["rows"]]
        tables = _table_pages(["Member", "Invited", "Current status", "Latest opt-in", "Latest opt-out"], data)
    if report["status"] == "not_found":
        message = "You have no recorded invitation for that member."
        return [{"text": message, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": message}}]}]
    if report["status"] == "ambiguous":
        message = "More than one person in your invitation history has that name. Use their Slack @mention."
        # The caller-owned candidates are safe to show below the clarification.
        opener = message
    if not tables:
        message = "You have not sponsored anyone yet."
        return [{"text": message, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": message}}]}]
    pages = []
    total = len(tables)
    for index, table in enumerate(tables, 1):
        lead = opener if index == 1 else f"Sponsorship register continued ({index} of {total})."
        plain_rows = [" | ".join(_fallback_cell(cell["text"]) for cell in row) for row in table]
        text = lead + "\n" + "\n".join(plain_rows)
        wrapped_columns = {0, 2} if len(table[0]) > 2 else {0}
        column_settings = [{"is_wrapped": column in wrapped_columns} for column in range(len(table[0]))]
        pages.append({"text": text, "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": lead}},
            {"type": "table", "rows": table, "column_settings": column_settings},
        ]})
    return pages
