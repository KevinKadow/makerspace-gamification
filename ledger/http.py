"""Small WSGI adapter around Bolt's verified HTTP request dispatcher."""
import json
import logging
import time
from urllib.parse import parse_qs
from slack_bolt.request import BoltRequest

LOG = logging.getLogger(__name__)
SLACK_PATHS = ("/slack/events", "/slack/commands", "/slack/interactions")


class HTTPApp:
    def __init__(self, bolt, store, sources=None):
        self.bolt, self.store, self.sources = bolt, store, sources

    def __call__(self, env, start_response):
        started = time.monotonic()
        path, method = env.get("PATH_INFO", "/"), env.get("REQUEST_METHOD", "GET")
        status, text, content_type = 404, "Not found", "text/plain"
        if path in ("/health", "/ready") and method == "GET":
            status, text = 200, "ok"
            if path == "/ready":
                try:
                    self.store.ready()
                    if self.sources is not None:
                        self.sources.ready()
                except Exception:
                    status, text = 503, "Database not ready"
        elif path in SLACK_PATHS and method == "POST":
            try:
                length = int(env.get("CONTENT_LENGTH", "0"))
                if length < 0 or length > 1024 * 1024:
                    status, text = 413, "Request too large"
                else:
                    body = env["wsgi.input"].read(length).decode("utf-8")
                    headers = {key[5:].lower().replace("_", "-"): val for key, val in env.items() if key.startswith("HTTP_")}
                    headers["content-type"] = env.get("CONTENT_TYPE", "")
                    result = self.bolt.dispatch(BoltRequest(body=body, headers=headers))
                    status, text = result.status, result.body
                    if path == "/slack/commands" and 200 <= status < 300:
                        # Bolt dispatch verifies Slack's signature before returning.
                        # Record only the human-readable slash-command fields, never
                        # the full form body (which also carries IDs and response URLs).
                        fields = parse_qs(body, keep_blank_values=True)
                        command = fields.get("command", [""])[0][:80]
                        human_text = fields.get("text", [""])[0][:1000]
                        LOG.info("Slack command command=%r text=%r", command, human_text)
                    content_type = result.headers.get("content-type", ["application/json"])
                    if isinstance(content_type, list):
                        content_type = content_type[0]
            except (ValueError, UnicodeDecodeError):
                status, text = 400, "Invalid request"
            except Exception as error:
                # Non-2xx is essential when durable ingress failed: Slack can retry.
                # Exception messages can contain credentials or request contents.
                LOG.error("Slack ingress failed error_type=%s", type(error).__name__)
                status, text = 503, "Please retry"
        elapsed_ms = (time.monotonic() - started) * 1000
        if path in SLACK_PATHS and (status >= 400 or elapsed_ms >= 2500):
            LOG.warning("Slack callback path=%s status=%s duration_ms=%.0f", path, status, elapsed_ms)
        if not isinstance(text, str):
            text = json.dumps(text)
        payload = text.encode("utf-8")
        reasons = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 413: "Payload Too Large", 503: "Service Unavailable"}
        start_response(f"{status} {reasons.get(status, 'Response')}", [("Content-Type", content_type), ("Content-Length", str(len(payload)))])
        return [payload]
