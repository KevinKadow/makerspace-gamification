"""Slack WebClient that records useful, privacy-aware API failures."""
import json
import logging
import math
import threading
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


log = logging.getLogger(__name__)
_SECRET_KEYS = {"token", "access_token", "authorization", "password", "secret", "cookie",
                "response_url", "trigger_id"}
_PRIVATE_CALL_KEYS = {"text", "blocks", "view", "file", "files", "attachments", "metadata",
                      "private_metadata", "initial_comment"}
_RESPONSE_HEADERS = {"content_type", "date", "retry_after", "x_slack_req_id",
                     "x_oauth_scopes", "x_accepted_oauth_scopes"}


def _key_name(value):
    return str(value).lower().replace("-", "_")


def _sanitize(value, *, response=False, key=None):
    normalized = _key_name(key) if key is not None else ""
    if normalized in _SECRET_KEYS:
        return "[redacted]"
    if not response and normalized in _PRIVATE_CALL_KEYS:
        if isinstance(value, str):
            return f"[redacted text; {len(value)} characters]"
        if isinstance(value, (list, tuple, dict)):
            return f"[redacted {type(value).__name__}; {len(value)} entries]"
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _sanitize(v, response=response, key=k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, response=response) for item in value]
    if isinstance(value, bytes):
        return f"[binary response; {len(value)} bytes]"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def retry_after_seconds(response, default=60):
    headers = getattr(response, "headers", {}) or {}
    value = next((v for k, v in headers.items() if _key_name(k) == "retry_after"), default)
    try:
        return max(0, math.ceil(float(value)))
    except (TypeError, ValueError):
        return default


class SlackCallDebugClient(WebClient):
    """A Bolt-compatible WebClient that logs API request/response details on errors."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rate_limit_lock = threading.Lock()
        self._rate_limited_until = 0.0

    def _wait_for_rate_limit(self):
        while True:
            with self._rate_limit_lock:
                remaining = self._rate_limited_until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(remaining)

    def _hold_calls_for(self, seconds):
        with self._rate_limit_lock:
            self._rate_limited_until = max(self._rate_limited_until, time.monotonic() + seconds)

    def api_call(self, api_method, **kwargs):
        self._wait_for_rate_limit()
        try:
            return super().api_call(api_method, **kwargs)
        except SlackApiError as exc:
            response = exc.response
            if getattr(response, "status_code", None) == 429:
                delay = retry_after_seconds(response)
                self._hold_calls_for(delay)
                log.warning("Slack rate limit active retry_after_seconds=%s", delay)
            data = getattr(response, "data", response)
            headers = getattr(response, "headers", {}) or {}
            diagnostic = {
                "http_verb": getattr(response, "http_verb", None),
                "api_url": getattr(response, "api_url", None),
                "status_code": getattr(response, "status_code", None),
                "headers": {key: value for key, value in headers.items()
                            if _key_name(key) in _RESPONSE_HEADERS},
                "data": _sanitize(data, response=True),
            }
            request = {"api_method": api_method, "kwargs": _sanitize(kwargs)}
            log.error("Slack API call failed method=%s request=%s response=%s",
                api_method, json.dumps(request, ensure_ascii=False, default=str),
                json.dumps(diagnostic, ensure_ascii=False, default=str))
            raise
