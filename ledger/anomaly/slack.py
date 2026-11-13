"""Anomaly analytics ingress, independent of Ledger game participation.

Every request is authorized again by the anomaly consumer before reading and
before delivery. Channel membership grants aggregate access only; source roles
are the authority for private staff detail and detector/schema administration.
"""
from collections.abc import Mapping
from hashlib import sha256
import os
import re

from ..domain import Denied
from ..storage import enqueue


ANOMALY_KINDS = ["anomaly_query"]
_USER = re.compile(r"[UW][A-Z0-9]+")
_CHANNEL = re.compile(r"[CG][A-Z0-9]+")
_DM = re.compile(r"D[A-Z0-9]+")
SECURITY_INTENT = re.compile(
    r"\b(?:security|sign[- ]?ups?|registrations?|bounc\w*|privilege\w*|MFA)\b|"
    r"\b(?:password|profile|reset)\b.{0,45}\b(?:changes?|edits?|requests?|attempts?|bursts?|rates?|activity|resets?)\b|"
    r"\bsimilar\w*\b.{0,30}\bemails?\b|\bemails?\b.{0,30}\bsimilar\w*\b|"
    r"\b(?:promot\w*|elevat\w*|grant\w*)\b.{0,40}\b(?:admin|board|resource.manager)\b", re.I)
_ANALYTICS = re.compile(
    SECURITY_INTENT.pattern + r"|"
    r"\b(?:anomal(?:y|ies|ous)|non[- ]?conform\w*|schema|baseline|drift|churn|"
    r"retention|revocations?|drop(?:ping)?[- ]?outs?)\b|"
    r"\b(?:subscri\w*|membership|members?)\b.{0,65}\b(?:cancel\w*|renew\w*|"
    r"expir\w*|revoked|long[- ]term|24 months|two years|at risk|stay\w*|leav\w*)\b|"
    r"\busers?\b.{0,65}\brevoked\b|\brevoked\b.{0,65}\b(?:members?|users?)\b|"
    r"\b(?:cancel\w*|renew\w*|expir\w*|long[- ]term|at risk)\b.{0,65}"
    r"\b(?:subscri\w*|membership|members?)\b|"
    r"\b(?:check[- ]?ins?|visits?|billing|payments?)\b.{0,60}"
    r"\b(?:patterns?|trends?|predict\w*|frequen\w*|issues?|fail\w*|season\w*)\b|"
    r"\b(?:patterns?|trends?|predict\w*|frequen\w*|season\w*)\b.{0,60}"
    r"\b(?:check[- ]?ins?|visits?|billing|payments?)\b",
    re.I,
)
_QUESTION = re.compile(r"\?|^\s*(?:<@[A-Z0-9]+>\s*)?(?:show|list|explain|analy[sz]e|compare|summari[sz]e|report|what|which|how|why|when|who|is|are|can|do|does|did|has|have)\b", re.I)


def anomaly_enabled():
    """Slack ingress is opt-in on each Ledger deployment."""
    return os.environ.get("ANOMALY_ENABLED", "false").strip().lower() == "true"


def _object(response, key):
    """SlackResponse and ordinary dictionaries share the public get interface."""
    if response.get("ok") is False:
        raise Denied("Anomaly access could not be verified.")
    value = response.get(key)
    if not isinstance(value, Mapping):
        raise Denied("Anomaly access could not be verified.")
    return value


class AnomalyAccess:
    def __init__(self, ledger, slack_client):
        self.ledger, self.slack = ledger, slack_client
        self._resolved_channel = None

    def _human(self, user_id):
        if not isinstance(user_id, str) or not _USER.fullmatch(user_id) or user_id == "USLACKBOT":
            raise Denied("Anomaly analytics requires an active human Slack identity.")
        user = _object(self.slack.users_info(user=user_id), "user")
        if (user.get("id") != user_id or user.get("deleted") is not False or
                user.get("is_bot") is not False or user.get("is_app_user") is True):
            raise Denied("Anomaly analytics requires an active human Slack identity.")

    def staff(self, user_id):
        self._human(user_id)
        member = self.ledger.sources.identity(user_id)
        return bool(isinstance(member, Mapping) and member.get("_id") is not None and
                    not member.get("merged_at") and
                    member.get("status") in ("activeMember", "pending", "inactive", "nonMember", "expired") and
                    member.get("role") in ("admin", "board_member"))

    def channel_id(self):
        configured = os.environ.get("SLACK_CHANNEL_ANOMALY", "ledger-audit").strip().removeprefix("#")
        if not configured:
            raise Denied("Configure SLACK_CHANNEL_ANOMALY before using anomaly analytics.")
        channel_id = self._resolved_channel
        if channel_id is None and _CHANNEL.fullmatch(configured):
            channel_id = configured
        if channel_id is None:
            cursor, seen, matches = None, set(), []
            for _ in range(100):
                response = self.slack.conversations_list(
                    types="public_channel,private_channel", exclude_archived=True, limit=200,
                    **({"cursor": cursor} if cursor else {}))
                channels = response.get("channels")
                metadata = response.get("response_metadata", {})
                if (response.get("ok") is False or not isinstance(channels, list) or
                        not isinstance(metadata, Mapping)):
                    raise Denied("Anomaly channel could not be resolved.")
                matches.extend(row.get("id") for row in channels
                               if isinstance(row, Mapping) and row.get("name") == configured)
                cursor = metadata.get("next_cursor", "")
                if not cursor:
                    break
                if not isinstance(cursor, str) or cursor in seen:
                    raise Denied("Anomaly channel inventory is incomplete.")
                seen.add(cursor)
            else:
                raise Denied("Anomaly channel inventory is incomplete.")
            if len(matches) != 1:
                raise Denied("Invite The Ledger to the configured anomaly channel and verify its name.")
            channel_id = matches[0]
        if not isinstance(channel_id, str) or not _CHANNEL.fullmatch(channel_id):
            raise Denied("Anomaly channel could not be resolved.")
        channel = _object(self.slack.conversations_info(channel=channel_id), "channel")
        if (channel.get("id") != channel_id or channel.get("is_member") is not True or
                channel.get("is_archived") is not False or channel.get("is_ext_shared") is True):
            raise Denied("The anomaly channel must be active, joined by The Ledger, and not externally shared.")
        if not _CHANNEL.fullmatch(configured) and channel.get("name") != configured:
            raise Denied("The configured anomaly channel name has changed.")
        self._resolved_channel = channel_id
        return channel_id

    def _channel_member(self, user_id, channel_id):
        cursor, seen = None, set()
        for _ in range(100):
            response = self.slack.conversations_members(
                channel=channel_id, limit=200, **({"cursor": cursor} if cursor else {}))
            members = response.get("members")
            metadata = response.get("response_metadata", {})
            if (response.get("ok") is False or not isinstance(members, list) or
                    not all(isinstance(value, str) for value in members) or
                    not isinstance(metadata, Mapping)):
                raise Denied("Anomaly channel membership could not be verified.")
            if user_id in members:
                return True
            cursor = metadata.get("next_cursor", "")
            if not cursor:
                return False
            if not isinstance(cursor, str) or cursor in seen:
                raise Denied("Anomaly channel membership is incomplete.")
            seen.add(cursor)
        raise Denied("Anomaly channel membership is incomplete.")

    def authorize(self, user_id, destination):
        self._human(user_id)
        if not isinstance(destination, str):
            raise Denied("Choose your private DM or the configured anomaly channel.")
        if _DM.fullmatch(destination):
            dm = _object(self.slack.conversations_info(channel=destination), "channel")
            if (dm.get("id") != destination or dm.get("is_im") is not True or
                    dm.get("user") != user_id or dm.get("is_mpim") is True):
                raise Denied("Private anomaly replies must use the caller's own direct message.")
            if self.staff(user_id):
                return "staff_detail"
            if self._channel_member(user_id, self.channel_id()):
                return "aggregate"
        elif _CHANNEL.fullmatch(destination):
            channel_id = self.channel_id()
            if destination == channel_id and self._channel_member(user_id, channel_id):
                return "aggregate"
        raise Denied("Anomaly analytics is available to admins, board members, and current anomaly-channel members.")


def queue_anomaly_request(ledger, slack, user_id, destination, text, request_id, thread_ts=None):
    """Persist once after authorization; the consumer must reauthorize at use."""
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError("Provide an anomaly question or command of 1–4,000 characters.")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 500:
        raise ValueError("Anomaly requests require a stable request identifier.")
    if thread_ts is not None and (not isinstance(thread_ts, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", thread_ts)):
        raise ValueError("Invalid Slack thread timestamp.")
    AnomalyAccess(ledger, slack).authorize(user_id, destination)
    key = "anomaly-query:" + sha256(f"{request_id}:{user_id}:{destination}".encode()).hexdigest()
    payload = {"user_id": user_id, "channel": destination, "text": text.strip(), "thread_ts": thread_ts}
    ledger.store.atomic(lambda store: enqueue(store, "ledger_outbox", key, "anomaly_query", payload))
    return key


def route_anomaly_event(ledger, slack, event):
    """Claim only known analytics intents, before broad counts/game consent.

Denied recognized requests must not fall through to a less restricted handler.
No ambient message history or membership enrollment is created here.
"""
    if not anomaly_enabled():
        return False
    if (event.get("type") not in ("message", "app_mention") or event.get("subtype") or
            event.get("bot_id") or event.get("bot_profile") or event.get("app_id")):
        return False
    text = event.get("text")
    if not isinstance(text, str) or not _ANALYTICS.search(text) or not _QUESTION.search(text):
        return False
    timestamp = event.get("ts")
    if not isinstance(timestamp, str) or not timestamp:
        raise Denied("Anomaly request is missing its Slack message identity.")
    queue_anomaly_request(ledger, slack, event.get("user"), event.get("channel"), text,
                          f"event:{event.get('channel')}:{timestamp}",
                          event.get("thread_ts") or timestamp)
    return True
