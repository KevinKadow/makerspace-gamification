"""Validated XML/Markdown policy; Google Docs refreshes stay outside transactions."""
from copy import deepcopy
from hashlib import sha256
import http.client
from importlib.resources import files
import json
import logging
import os
import re
import socket
from threading import RLock, Timer
import time
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

MAX_MATRIX_BYTES = 32768
MATRIX_FILE = "prompt_matrix.xml.md"
REQUIRED_SECTIONS = {"identity", "authority", "roles", "consent", "channels", "progression", "economy",
                     "kudos", "community", "privacy", "response", "examples"}
REQUIRED_ROLES = {"ledger", "member", "participant", "nonparticipant", "sponsor", "success_buddy", "mentor",
                  "checkout_approver", "admin", "board_member", "resource_manager", "tool_captain",
                  "workshop_instructor", "design_challenge_judge", "quest_author", "ai_observer", "delegated_reviewer",
                  "ledger_quest_author"}
log = logging.getLogger(__name__)


def validate_matrix(text, source="bundled"):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_MATRIX_BYTES:
        raise ValueError("Prompt matrix must be UTF-8 text no larger than 32 KiB")
    text = text.lstrip("\ufeff").strip().replace("\r\n", "\n")
    if re.search(r"<!\s*(DOCTYPE|ENTITY)\b", text, re.I):
        raise ValueError("DTD/entity declarations are not allowed")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError("Prompt matrix must be complete XML with Markdown in CDATA") from exc
    if (root.tag != "prompt_matrix" or root.get("schema_version") != "1" or root.get("id") != "the-ledger"
            or not re.fullmatch(r"[1-9][0-9]{0,8}", root.get("version", ""))):
        raise ValueError("Invalid matrix identity/schema/version")
    if len(root) != len(REQUIRED_SECTIONS) or {e.tag for e in root} != REQUIRED_SECTIONS:
        raise ValueError("Matrix sections must be complete and unique")
    roles = root.find("roles")
    identifiers = [r.get("id", "") for r in roles]
    if (len(identifiers) != len(set(identifiers)) or not REQUIRED_ROLES.issubset(identifiers)
            or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", i) for i in identifiers)):
        raise ValueError("Matrix requires unique IDs and all required roles")
    for node in root:
        if node.tag == "roles":
            if any(r.tag != "role" or len(r) or not (r.text or "").strip() for r in node):
                raise ValueError("Each role needs a plain Markdown description")
        elif len(node) or not (node.text or "").strip():
            raise ValueError("Every section needs plain Markdown content")
    return {"text": text, "version": root.get("version"), "sha256": sha256(text.encode()).hexdigest(), "source": source}


def bundled_matrix():
    return validate_matrix(files("ledger").joinpath("prompts", MATRIX_FILE).read_text(encoding="utf-8"))


def google_doc_id(url):
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname != "docs.google.com" or parsed.port not in (None, 443)
            or parsed.username or parsed.password):
        raise ValueError("Use an HTTPS docs.google.com document URL")
    match = re.fullmatch(r"/document/(?:u/[0-9]+/)?d/([A-Za-z0-9_-]{8,})(?:/(?:edit|view|preview|export))?/?", parsed.path)
    if not match:
        raise ValueError("Use a normal Google Doc link, not a published /d/e/ page")
    return match.group(1)


def allowed_download(url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return (parsed.scheme == "https" and parsed.port in (None, 443) and not parsed.username and not parsed.password
            and (host in ("docs.google.com", "www.googleapis.com", "drive.google.com", "drive.usercontent.google.com")
                 or host.endswith(".googleusercontent.com")))


def fetch_google_doc(url, access_token="", deadline=15):
    """Export plain text, with a total deadline and Google-only redirect destinations."""
    identifier = google_doc_id(url)
    if "\r" in access_token or "\n" in access_token:
        raise ValueError("Invalid Google access token")
    url = (f"https://www.googleapis.com/drive/v3/files/{identifier}/export?mimeType=text%2Fplain" if access_token
           else f"https://docs.google.com/document/d/{identifier}/export?format=txt")
    started = time.monotonic()
    for hop in range(4):
        if not allowed_download(url) or time.monotonic() - started >= deadline:
            raise ValueError("Invalid or timed-out document export")
        parsed = urlparse(url)
        connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=min(2, deadline - (time.monotonic() - started)))
        timer = None
        try:
            connection.connect()
            sock = connection.sock
            def expire(sock=sock):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            remaining = max(0.01, deadline - (time.monotonic() - started))
            sock.settimeout(remaining)
            timer = Timer(remaining, expire)
            timer.daemon = True
            timer.start()
            headers = {"Accept": "text/plain", "Cache-Control": "no-cache"}
            if access_token and hop == 0:
                headers["Authorization"] = "Bearer " + access_token
            connection.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""), headers=headers)
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Missing document redirect")
                url = urljoin(url, location)
                continue
            if response.status != 200 or response.getheader("Content-Type", "").split(";")[0].strip().lower() != "text/plain":
                raise ValueError("Document export must return successful plain text")
            chunks, size = [], 0
            while True:
                chunk = response.read1(4096)
                if time.monotonic() - started >= deadline:
                    raise TimeoutError("Document export deadline exceeded")
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_MATRIX_BYTES:
                    raise ValueError("Document export exceeds 32 KiB")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8-sig")
        finally:
            if timer:
                timer.cancel()
            connection.close()
    raise ValueError("Too many document redirects")


class PromptMatrix:
    def __init__(self, url="", access_token=""):
        self.url, self.access_token = url, access_token
        self._current = bundled_matrix()
        self._lock = RLock()
        self._attempted, self._revision = False, None
        self.outcome = "bundled"

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("LEDGER_PROMPT_MATRIX_DOC_URL", "").strip(),
                   os.environ.get("LEDGER_PROMPT_MATRIX_GOOGLE_ACCESS_TOKEN", ""))

    def snapshot(self):
        with self._lock:
            return deepcopy(self._current)

    def refresh(self, revision="startup", force=False):
        with self._lock:
            if self._attempted and revision == self._revision and not force:
                return self.status()
            try:
                candidate = validate_matrix(fetch_google_doc(self.url, self.access_token), "google_doc") if self.url else bundled_matrix()
                self._current = candidate
                self.outcome = "loaded"
            except (OSError, ValueError, http.client.HTTPException):
                self.outcome = "retained" if self._current["source"] == "google_doc" else "bundled_fallback"
                log.warning("Prompt matrix refresh failed; %s retained. Check document access and XML structure.", self._current["source"])
            self._attempted, self._revision = True, revision
            return self.status()

    def status(self):
        with self._lock:
            return {**{k: v for k, v in self._current.items() if k != "text"}, "outcome": self.outcome,
                    "bytes": len(self._current["text"].encode())}


if __name__ == "__main__":
    # Local validation only. `ledger prompt-matrix` also checks the configured Doc.
    policy = bundled_matrix()
    print(json.dumps({k: v for k, v in policy.items() if k != "text"}))
