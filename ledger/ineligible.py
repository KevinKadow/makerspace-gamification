"""Configured Slack identities excluded from Ledger participation."""
import json
import os
import re


_SLACK_ID = re.compile(r"[UW][A-Z0-9]+\Z")


def parse_slackids_ineligible(value=None):
    """Parse JSON arrays or the documented brace-delimited quoted ID form."""
    raw = os.environ.get("SLACKIDS_INELIGIBLE", "") if value is None else value
    raw = raw.strip()
    if not raw or raw in ("{}", "[]"):
        return frozenset()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        if not (raw.startswith("{") and raw.endswith("}")):
            raise ValueError("SLACKIDS_INELIGIBLE must be a JSON array or quoted-ID set")
        try:
            parsed = json.loads("[" + raw[1:-1] + "]")
        except json.JSONDecodeError as exc:
            raise ValueError("SLACKIDS_INELIGIBLE contains invalid quoted IDs") from exc
    if not isinstance(parsed, list) or any(not isinstance(item, str) or not _SLACK_ID.fullmatch(item)
                                           for item in parsed):
        raise ValueError("SLACKIDS_INELIGIBLE must contain only Slack user IDs")
    return frozenset(parsed)


def is_ineligible_slack_id(slack_id):
    return isinstance(slack_id, str) and slack_id in parse_slackids_ineligible()


def is_ineligible_member(ledger, member_id):
    return is_ineligible_slack_id(ledger.sources.slack_id(member_id))
