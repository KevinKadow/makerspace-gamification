"""Private, generation-checked retrieval over distilled public Slack threads.

The Mongo record is the authority for publication. Chroma is only an index:
stale, deleted, quarantined, or partially replaced vectors never become evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timezone

import requests

COLLECTION = "ledger_knowledge"
_CHANNEL = re.compile(r"C[A-Z0-9]+\Z")
_TIMESTAMP = re.compile(r"[0-9]{1,16}\.[0-9]{1,6}\Z")
_USER = re.compile(r"[UW][A-Z0-9]+\Z")


class PrivateEmbeddings:
    """LangChain embeddings backed by the private TEI HTTP service."""
    def __init__(self, endpoint):
        self.endpoint = endpoint.rstrip("/")

    def _embed(self, inputs, read_timeout=8):
        response = requests.post(self.endpoint + "/embed", json={"inputs": inputs}, timeout=(2, read_timeout))
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, list) or len(result) != len(inputs):
            raise ValueError("Invalid embedding response")
        if not all(isinstance(row, list) and row and all(isinstance(x, (float, int)) and math.isfinite(x)
                                                         for x in row) for row in result):
            raise ValueError("Invalid embedding vector")
        return result

    def embed_documents(self, texts):
        # TEI defaults to 32 inputs per request.
        result = []
        for start in range(0, len(texts), 32):
            result.extend(self._embed(["search_document: " + text for text in texts[start:start + 32]], read_timeout=45))
        return result

    def embed_query(self, text):
        return self._embed(["search_query: " + text])[0]


def _timestamp(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()
    if isinstance(value, (float, int)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
            except ValueError:
                return None
    return None


def thread_url(channel, ts):
    """Build a Slack link from validated IDs, never from exported message text."""
    if not isinstance(channel, str) or not _CHANNEL.fullmatch(channel) or not isinstance(ts, str) or not _TIMESTAMP.fullmatch(ts):
        return None
    whole, fraction = ts.split(".")
    return f"https://app.slack.com/archives/{channel}/p{whole}{fraction.ljust(6, '0')}"


def summary_text(summary):
    if not isinstance(summary, dict):
        raise ValueError("Missing distilled summary")
    labels = (("Topic", "topic"), ("Participants", "participants"), ("Problem/Context", "problem_context"),
              ("Resolution/Decision", "resolution_decision"), ("Action Items", "action_items"))
    lines = []
    for label, key in labels:
        value = summary.get(key, "")
        if isinstance(value, list):
            value = "; ".join(str(item) for item in value)
        if not isinstance(value, str):
            raise ValueError("Invalid summary field")
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _participant_key(value):
    # Chroma 1.0 supports scalar metadata but not list membership. A stable
    # one-way key per name/ID gives it an exact pre-search boolean filter.
    return "participant_" + hashlib.sha256(value.strip().casefold().encode()).hexdigest()


class _LiveHumanVerifier:
    """Bound Slack account checks; send only an ID and the bot credential."""
    def __init__(self, token, deadline=None):
        self.token = token
        self.remaining = 5
        self.deadline = min(time.monotonic() + 8, deadline) if deadline is not None else time.monotonic() + 8

    def __call__(self, uid):
        remaining = self.deadline - time.monotonic()
        if not self.token or self.remaining <= 0 or remaining <= 0.1 or uid == "USLACKBOT":
            return False
        self.remaining -= 1
        per_phase = min(1.0, remaining / 2)
        try:
            response = requests.get("https://slack.com/api/users.info", params={"user": uid},
                                    headers={"Authorization": f"Bearer {self.token}"}, timeout=(per_phase, per_phase))
            response.raise_for_status()
            payload = response.json()
            user = payload.get("user") if payload.get("ok") is True else None
            return bool(isinstance(user, dict) and user.get("id") == uid
                        and user.get("deleted") is False and user.get("is_bot") is False)
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            return False


def _role_boost(sources, participants, clock, cache=None, store=None, verify_human=None):
    """Current verified identity is ranking evidence, never authorization."""
    if sources is None:
        return 0.0
    best = 0.0
    cache = cache if cache is not None else {}
    for uid in list(participants or ())[:30]:
        if not isinstance(uid, str) or not _USER.fullmatch(uid):
            continue
        if uid in cache:
            best = max(best, cache[uid])
            if best >= 0.10:
                return best
            continue
        if len(cache) >= 20:
            break
        cache[uid] = 0.0
        member = sources.identity(uid)
        if not member:
            continue
        if member.get("status") in ("suspended", "revoked"):
            continue
        staff = member.get("role") in ("admin", "board_member", "resource_manager")
        expiry = _timestamp(member.get("expirationTime"))
        # Rails stores member expiry in milliseconds.
        if expiry is not None and expiry > 1e11:
            expiry /= 1000
        subscriber = member.get("status") == "activeMember" and expiry is not None and expiry > clock
        subscribed = subscriber and bool(member.get("subscription") or member.get("subscription_id"))
        if subscriber and not subscribed and member.get("groupName"):
            groups = sources.bounded("groups", {"groupName": member["groupName"]},
                                     ["subscription", "subscription_id", "expiry"], 20)
            for group in groups:
                group_expiry = _timestamp(group.get("expiry"))
                if group_expiry is not None and group_expiry > 1e11:
                    group_expiry /= 1000
                if (group.get("subscription") or group.get("subscription_id")) and (group_expiry is None or group_expiry > clock):
                    subscribed = True
                    break
        if not staff and not subscribed:
            continue
        # Source links establish membership but not Slack account status. Use
        # only a recent users.info/user-change check for this exact link, or a
        # bounded live read. Missing, inactive or unverifiable users get no boost.
        member_id = member.get("_id")
        state = store.get("ledger_catalog", f"identity:{member_id}") if store and member_id is not None else None
        checked = _timestamp(state.get("at")) if state else None
        fresh = bool(state and state.get("slack_id") == uid and checked is not None and 0 <= clock - checked <= 86400)
        verified = (state.get("deactivated") is False and state.get("bot") is False) if fresh else bool(verify_human and verify_human(uid))
        if not verified:
            continue
        cache[uid] = 0.10 if staff else 0.04
        best = max(best, cache[uid])
        if staff:
            return best
    return best


class KnowledgeIndex:
    def __init__(self, store, sources, vectors, splitter=None, verify_human_factory=None):
        self.store, self.sources, self.vectors, self.splitter = store, sources, vectors, splitter
        self.verify_human_factory = verify_human_factory

    @classmethod
    def from_env(cls, store, sources=None):
        # Imported lazily so ordinary Ledger operations work without knowledge extras.
        import chromadb
        from chromadb.config import Settings
        from langchain_chroma import Chroma
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        client = chromadb.HttpClient(host=os.getenv("LEDGER_CHROMA_HOST", "ledger-chroma"),
                                    port=int(os.getenv("LEDGER_CHROMA_PORT", "8000")),
                                    settings=Settings(anonymized_telemetry=False))
        vectors = Chroma(client=client, collection_name="ledger_slack_knowledge_v1",
                         collection_metadata={"hnsw:space": "cosine"},
                         embedding_function=PrivateEmbeddings(os.getenv("LEDGER_EMBEDDING_URL", "http://ledger-embeddings:80")))
        splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(chunk_size=1600, chunk_overlap=120)
        return cls(store, sources, vectors, splitter,
                   verify_human_factory=lambda deadline: _LiveHumanVerifier(os.getenv("SLACK_BOT_TOKEN", ""), deadline))

    def publish(self, record):
        if record.get("status") != "ready" or not record.get("generation") or not record.get("_id"):
            raise ValueError("Only complete, versioned threads may be indexed")
        if not thread_url(record.get("channel_id"), record.get("thread_ts")):
            raise ValueError("Invalid source thread")
        text = summary_text(record.get("summary"))
        chunks = self.splitter.split_text(text) if self.splitter else [text]
        if not chunks:
            raise ValueError("Empty knowledge document")
        metadata = {"thread_id": str(record["_id"]), "generation": str(record["generation"]),
                    "channel_id": record["channel_id"], "channel_name": str(record.get("channel_name") or ""),
                    "date_start": str(record.get("date_start") or ""), "date_end": str(record.get("date_end") or ""),
                    "participants": json.dumps(record.get("participant_ids") or []), "thread_ts": record["thread_ts"]}
        start, end = _timestamp(record.get("date_start")), _timestamp(record.get("date_end"))
        if start is None or end is None:
            raise ValueError("Invalid knowledge date range")
        metadata.update(date_start_epoch=start, date_end_epoch=end)
        participants = list(record.get("participant_ids") or []) + [row.get("display_name") for row in record.get("messages", [])]
        for participant in participants:
            if isinstance(participant, str) and participant.strip():
                metadata[_participant_key(participant)] = True
        ids = [hashlib.sha256(f"{record['_id']}\0{record['generation']}\0{i}".encode()).hexdigest()
               for i in range(len(chunks))]
        self.vectors.add_texts(chunks, metadatas=[metadata] * len(chunks), ids=ids)
        current = self.store.get(COLLECTION, record["_id"])
        if not current or current.get("status") != "ready" or current.get("generation") != record["generation"]:
            # A removal or replacement may have completed during the embedding
            # request. Only delete our own generation; never delete newer work.
            self.vectors.delete(where={"$and": [{"thread_id": str(record["_id"])},
                                                {"generation": str(record["generation"])}]})
            raise RuntimeError("Knowledge publication was superseded")
        # Do not delete other generations here: a delayed publisher could race a
        # newer one. Every read checks Mongo's current published generation.
        # Explicit removal deletes every generation of this thread.

    def remove(self, thread_id):
        self.vectors.delete(where={"thread_id": str(thread_id)})

    def retrieve(self, question, *, limit=4, channel_name=None, date_from=None, date_to=None, participant=None, min_similarity=0.60, deadline=None):
        if not isinstance(question, str) or not question.strip() or len(question) > 2500:
            raise ValueError("Invalid knowledge query")
        if not 1 <= limit <= 10 or not 0 <= min_similarity <= 1:
            raise ValueError("Invalid retrieval bounds")
        if participant is not None and (not isinstance(participant, str) or not participant.strip() or len(participant) > 100):
            raise ValueError("Invalid participant filter")
        if date_from is not None and _timestamp(date_from) is None or date_to is not None and _timestamp(date_to) is None:
            raise ValueError("Invalid date filter")
        start_filter = _timestamp(date_from) if date_from is not None else None
        end_filter = _timestamp(date_to) if date_to is not None else None
        if isinstance(date_to, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_to):
            end_filter += 86400 - 1e-6
        kwargs = {"k": 50}
        filters = []
        if channel_name:
            filters.append({"channel_name": channel_name})
        if participant:
            filters.append({_participant_key(participant): True})
        if start_filter is not None:
            filters.append({"date_end_epoch": {"$gte": start_filter}})
        if end_filter is not None:
            filters.append({"date_start_epoch": {"$lte": end_filter}})
        if filters:
            kwargs["filter"] = filters[0] if len(filters) == 1 else {"$and": filters}
        candidates = self.vectors.similarity_search_with_score(question, **kwargs)
        clock = datetime.now(timezone.utc).timestamp()
        result = {}
        identity_cache = {}
        verify_human = self.verify_human_factory(deadline) if self.verify_human_factory else None
        for doc, distance in candidates:
            similarity = 1.0 - float(distance)  # collection uses cosine distance
            if not math.isfinite(similarity) or similarity < min_similarity:
                continue
            metadata = doc.metadata
            record = self.store.get(COLLECTION, metadata.get("thread_id"))
            if (not record or record.get("status") != "ready" or record.get("generation") != metadata.get("generation")
                    or record.get("indexed_generation") != record.get("generation")):
                continue
            url = thread_url(record.get("channel_id"), record.get("thread_ts"))
            if not url or channel_name and record.get("channel_name") != channel_name:
                continue
            if participant:
                names = [m.get("display_name", "") for m in record.get("messages", [])]
                ids = record.get("participant_ids") or []
                if participant.strip().casefold() not in {str(value).strip().casefold() for value in [*names, *ids]}:
                    continue
            start, end = _timestamp(record.get("date_start")), _timestamp(record.get("date_end"))
            if start is None or end is None:
                continue
            if start_filter is not None and end < start_filter or end_filter is not None and start > end_filter:
                continue
            freshness = 0.25 * 2 ** (-max(0.0, clock - end) / (180 * 86400))
            boost = _role_boost(self.sources, record.get("participant_ids"), clock, identity_cache, self.store, verify_human)
            score = similarity + freshness + boost
            item = {"text": doc.page_content, "channel_id": record["channel_id"],
                    "channel_name": record.get("channel_name") or record["channel_id"],
                    "thread_ts": record["thread_ts"], "date_start": record["date_start"],
                    "date_end": record["date_end"], "participants": record.get("summary", {}).get("participants", []),
                    "thread_id": record["_id"], "generation": record["generation"],
                    "url": url, "score": score, "similarity": similarity}
            key = record["_id"]
            if key not in result or score > result[key]["score"]:
                result[key] = item
        return sorted(result.values(), key=lambda item: item["score"], reverse=True)[:limit]

    def valid_evidence(self, evidence):
        """Fail closed when a saved response's supporting thread was replaced or removed."""
        if not isinstance(evidence, list) or not evidence:
            return False
        for item in evidence:
            if not isinstance(item, dict) or not item.get("thread_id") or not item.get("generation"):
                return False
            current = self.store.get(COLLECTION, item["thread_id"])
            if (not current or current.get("status") != "ready" or current.get("generation") != item["generation"]
                    or current.get("indexed_generation") != current.get("generation")):
                return False
            if thread_url(current.get("channel_id"), current.get("thread_ts")) != item.get("url"):
                return False
        return True
