"""Bounded native Metabot client and explicit, repeatable deployment bootstrap.

No user-supplied SQL, database connection, URL, or conversation ID belongs here:
the authorized anomaly router supplies the conversation and destination.
"""
from __future__ import annotations

import json
import time
from collections.abc import Mapping
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import UUID, uuid4

import requests


class MetabaseError(RuntimeError):
    """A sanitized failure suitable for operational logs."""


def _uuid(value):
    return str(UUID(str(value)))


def _base_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("Metabase URL must be an HTTP(S) origin without credentials or a path")
    return value.rstrip("/")


def _reference(value):
    if not isinstance(value, dict):
        raise ValueError("Chart reference must be an object")
    if value.get("type") == "saved_card" and set(value) == {"type", "card_id"}:
        if type(value["card_id"]) is int and value["card_id"] > 0:
            return dict(value)
    if value.get("type") == "conversation_chart" and set(value) == {"type", "conversation_id", "chart_id"}:
        return {"type": "conversation_chart", "conversation_id": _uuid(value["conversation_id"]),
                "chart_id": _uuid(value["chart_id"])}
    raise ValueError("Unsupported chart reference")


class MetabaseClient:
    def __init__(self, base_url, chat_key, chart_key, timeout=120, *, session=None):
        self.base_url = _base_url(base_url)
        self.chat_key, self.chart_key = chat_key, chart_key
        self.timeout = max(1, min(float(timeout), 300))
        self.session = session or requests.Session()

    def _request(self, method, path, *, key=None, allow_missing=False, **kwargs):
        # All paths are generated in this module; never follow redirects with credentials.
        try:
            response = self.session.request(
                method, self.base_url + path,
                headers={"X-API-Key": key or self.chat_key},
                timeout=(5, self.timeout), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise MetabaseError("Metabase is unavailable") from None
        if allow_missing and response.status_code == 404:
            response.close()
            return None
        if not 200 <= response.status_code < 300:
            status = response.status_code
            response.close()
            raise MetabaseError(f"Metabase request failed (HTTP {status})")
        return response

    def _json(self, method, path, *, key=None, allow_missing=False, **kwargs):
        response = self._request(method, path, key=key, allow_missing=allow_missing, **kwargs)
        if response is None:
            return None
        try:
            if response.status_code == 204:
                return None
            return response.json()
        except ValueError:
            raise MetabaseError("Metabase returned invalid JSON") from None
        finally:
            response.close()

    def ask(self, message, conversation_id, context=None):
        """Run one native turn. Caller must durably bind and serialize each conversation.

        Native parent IDs are recovered from persisted state, so process restarts
        do not create a new conversation or splice unrelated history into a turn.
        A 409 is surfaced to the caller rather than replaying a possibly accepted turn.
        """
        conversation_id = _uuid(conversation_id)
        if not isinstance(message, str) or not message.strip() or len(message) > 8000:
            raise ValueError("Question must contain 1–8000 characters")
        context = dict(context or {})
        if set(context) - {"default_database_id", "current_time_with_timezone"}:
            raise ValueError("Only curated database and current-time context are supported")
        if "default_database_id" in context and (type(context["default_database_id"]) is not int or context["default_database_id"] <= 0):
            raise ValueError("Invalid curated database ID")
        previous = self._json("GET", f"/api/metabot/conversations/{conversation_id}", allow_missing=True)
        agents = [m for m in (previous or {}).get("messages", []) if m.get("role") == "agent"]
        if agents and agents[-1].get("status", {}).get("type") == "in_progress":
            raise MetabaseError("A turn is already running for this conversation")
        payload = {"profile_id": "anomaly-analytics", "message": message,
                   "context": context, "conversation_id": conversation_id,
                   "user_message_id": str(uuid4()), "assistant_message_id": str(uuid4())}
        if agents:
            payload["parent_message_id"] = _uuid(agents[-1].get("externalId") or agents[-1]["id"])
        response = self._request("POST", "/api/metabot/agent-streaming", json=payload, stream=True)
        chunks, refs, size, finished = [], [], 0, False
        started = time.monotonic()
        try:
            if "text/event-stream" not in response.headers.get("Content-Type", ""):
                raise MetabaseError("Metabase returned an unexpected chat content type")
            for line in response.iter_lines(decode_unicode=True):
                if time.monotonic() - started > self.timeout:
                    raise MetabaseError("Metabase chat exceeded its time limit")
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                size += len((line or "").encode("utf-8"))
                if size > 2 * 1024 * 1024:
                    raise MetabaseError("Metabase chat exceeded its response limit")
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    event = json.loads(raw)
                except ValueError:
                    raise MetabaseError("Metabase returned malformed chat data") from None
                if not isinstance(event, dict):
                    raise MetabaseError("Metabase returned malformed chat data")
                kind, data = event.get("type"), event.get("data", {})
                if kind == "text-delta":
                    chunks.append(str(event.get("delta", "")))
                elif kind == "error":
                    raise MetabaseError("Metabase could not complete this analysis")
                elif kind == "finish":
                    if event.get("finishReason") != "stop":
                        raise MetabaseError("Metabase analysis was incomplete")
                    finished = True
                else:
                    try:
                        if kind == "data-generated_entity" and isinstance(data, dict) and data.get("type") == "card":
                            refs.append(_reference({"type": "conversation_chart", "conversation_id": conversation_id,
                                                    "chart_id": data.get("id")}))
                        elif kind == "data-static_viz" and isinstance(data, dict):
                            refs.append(_reference({"type": "saved_card", "card_id": data.get("entity_id")}))
                    except ValueError:
                        raise MetabaseError("Metabase returned an invalid chart reference") from None
            if not finished:
                raise MetabaseError("Metabase chat ended before completion")
        except requests.RequestException:
            raise MetabaseError("Metabase chat connection was interrupted") from None
        finally:
            response.close()
        unique = list({json.dumps(ref, sort_keys=True): ref for ref in refs}.values())[:4]
        return {"answer": "".join(chunks).strip(), "chartrefs": unique,
                "conversation_id": conversation_id, "assistant_message_id": payload["assistant_message_id"]}

    def render(self, reference):
        """Download a checked PNG; return None only for an unavailable supported chart."""
        reference = _reference(reference)
        # A conversation belongs to the chat key's synthetic user. The saved-card
        # export key can be independently scoped, but cannot impersonate that user.
        key = self.chat_key if reference["type"] == "conversation_chart" else self.chart_key
        response = self._request("POST", "/api/metabot/analytics/render", key=key,
                                 json=reference, stream=True, allow_missing=True)
        if response is None:
            return None
        started = time.monotonic()
        try:
            if response.headers.get("Content-Type", "").split(";", 1)[0] != "image/png":
                raise MetabaseError("Metabase returned an unexpected chart content type")
            content = bytearray()
            for chunk in response.iter_content(65536):
                if time.monotonic() - started > self.timeout:
                    raise MetabaseError("Metabase chart download exceeded its time limit")
                content.extend(chunk)
                if len(content) > 8 * 1024 * 1024:
                    raise MetabaseError("Metabase chart exceeded 8 MiB")
            if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                raise MetabaseError("Metabase chart is not a PNG")
            return bytes(content)
        except requests.RequestException:
            raise MetabaseError("Metabase chart download was interrupted") from None
        finally:
            response.close()


def resolved_mongo_uri(uri, database=None):
    """Apply MLAB_DATABASE without losing SRV, authSource, or percent-encoded credentials."""
    parsed = urlsplit(uri)
    if parsed.scheme not in {"mongodb", "mongodb+srv"} or not parsed.netloc or parsed.fragment:
        raise ValueError("MLAB_URI must be a MongoDB connection URI")
    db = database or parsed.path.lstrip("/") or "makerauth"
    if not db or any(c in db for c in '/\\\x00"$ '):
        raise ValueError("Invalid MLAB_DATABASE")
    return urlunsplit((parsed.scheme, parsed.netloc, "/" + quote(db, safe="%"), parsed.query, ""))


def _items(value):
    return value.get("data", []) if isinstance(value, dict) else value


def _connection_matches(database, engine, expected):
    """Compare effective setup details without trusting redacted credentials.

    Extra JDBC properties can override even explicitly supplied TLS/host values.
    Only known inert UI options and disabled connection options may be added.
    """
    actual = database.get("details")
    if database.get("engine") != engine or not isinstance(actual, Mapping):
        return False
    if any(database.get(key) for key in ("write_data_details", "admin_details")):
        return False
    actual = dict(actual)
    if engine == "postgres":
        port = actual.get("port")
        if isinstance(port, str) and port.isascii() and port.isdecimal():
            actual["port"] = int(port)
        # Metabase never returns the stored reporting password.
        actual.pop("password", None)
        expected = {key: value for key, value in expected.items() if key != "password"}
    if any(type(actual.get(key)) is not type(value) or actual.get(key) != value
           for key, value in expected.items()):
        return False
    defaults = {"tunnel-enabled": False}
    inert_booleans = {"advanced-options", "json-unfolding", "let-user-control-scheduling", "auto_run_queries", "refingerprint"}
    if engine == "postgres":
        defaults.update({"ssl-mode": "require", "ssl-use-client-auth": False,
                         "use-auth-provider": False, "additional-options": ""})
    else:
        # Mongo selects a separate dbname before the URI's database, if nonempty.
        defaults["dbname"] = ""
        inert_booleans.add("ssl")  # Ignored by the Mongo driver in URI mode.
    for key in actual.keys() - expected.keys():
        value = actual[key]
        if key in inert_booleans and (value is None or type(value) is bool):
            continue
        if key in defaults and (value is None or (type(value) is type(defaults[key]) and value == defaults[key])):
            continue
        return False
    return True


def _ensure_raw_source(client, uri):
    name = "Makerspace Source (read only)"
    matches = [row for row in _items(client._json("GET", "/api/database")) if row.get("name") == name]
    if len(matches) > 1:
        raise MetabaseError(f"Duplicate bootstrap objects named {name}")
    details = {"use-conn-uri": True, "conn-uri": uri}
    body = {"engine": "mongo", "details": details, "is_full_sync": False}
    masked = "**MetabasePass**"  # Native API secret/protected-password marker.
    if matches:
        database = matches[0]
        masked_details = {**details, "conn-uri": masked}
        if _connection_matches(database, "mongo", masked_details):
            # A mask proves nothing about the old target or credentials. Explicit
            # bootstrap reestablishes the configured URI; PUT failures abort setup.
            database = client._json("PUT", f"/api/database/{database['id']}", json=body)
        elif not _connection_matches(database, "mongo", details):
            raise MetabaseError("Existing raw source does not match the configured read-only connection")
        else:
            return database
    else:
        database = client._json("POST", "/api/database", json={"name": name, **body})
    # A redacted response is safe only after our own successful connection write.
    if not (_connection_matches(database, "mongo", details) or
            _connection_matches(database, "mongo", {**details, "conn-uri": masked})):
        raise MetabaseError("Existing raw source does not match the configured read-only connection")
    return database


def _notebook_query(query):
    """Cards accept legacy MBQL on creation but this fork returns MBQL 5."""
    if not isinstance(query, dict):
        return False
    if query.get("type") == "query":
        return isinstance(query.get("query"), dict) and "native" not in query
    stages = query.get("stages")
    return (query.get("lib/type") == "mbql/query" and isinstance(stages, list) and bool(stages)
            and all(isinstance(stage, dict) and stage.get("lib/type") == "mbql.stage/mbql" for stage in stages))


def bootstrap(config: Mapping[str, str]):
    """Explicit setup command only; never called during worker startup.

    Requires existing Metabase admin API keys. Populated keys/URIs are never
    returned. Reports IDs so operators can establish group permissions.
    Re-runs reuse exact owned names and do not rewrite existing cards/dashboards.
    """
    key = config["ANOMALY_METABASE_BOOTSTRAP_API_KEY"]
    curated = MetabaseClient(config["ANOMALY_METABASE_URL"], key, key)
    existing_databases = _items(curated._json("GET", "/api/database"))
    if any(database.get("name") != "Ledger Anomaly Reporting" for database in existing_databases):
        raise MetabaseError("Curated instance contains another data source; use a dedicated reporting-only instance")

    def ensure(client, endpoint, name, body):
        matches = [r for r in _items(client._json("GET", endpoint)) if r.get("name") == name]
        if len(matches) > 1:
            raise MetabaseError(f"Duplicate bootstrap objects named {name}")
        return matches[0] if matches else client._json("POST", endpoint, json={"name": name, **body})

    details = {"host": config["ANOMALY_REPORTING_DB_HOST"],
               "port": int(config.get("ANOMALY_REPORTING_DB_PORT", "5432")),
               "dbname": config.get("ANOMALY_REPORTING_DB_NAME", "anomaly"),
               "user": config["ANOMALY_REPORTING_DB_USER"],
               "password": config["ANOMALY_REPORTING_DB_PASSWORD"],
               "ssl": config.get("ANOMALY_REPORTING_DB_SSL", "false").lower() == "true",
               "schema-filters-type": "inclusion", "schema-filters-patterns": "reporting"}
    database = ensure(curated, "/api/database", "Ledger Anomaly Reporting",
                      {"engine": "postgres", "details": details, "is_full_sync": True})
    if not _connection_matches(database, "postgres", details):
        raise MetabaseError("Existing reporting source does not match the configured read-only connection")
    db_id = database["id"]
    curated._json("PUT", "/api/setting/anomaly-analytics-database-ids", json={"value": [db_id]})
    # Native UI and the engine both use a tool profile without mutations.
    curated._json("PUT", "/api/setting/anomaly-analytics-only", json={"value": True})
    metadata = curated._json("GET", f"/api/database/{db_id}/metadata")
    tables = {t["name"]: t for t in metadata.get("tables", []) if t.get("schema") == "reporting"}
    required = {"cohort_metrics", "findings_summary", "pipeline_health", "model_quality"}
    if not required.issubset(tables):
        response = curated._request("POST", f"/api/database/{db_id}/sync_schema")
        response.close()
        raise MetabaseError("Reporting schema sync requested; rerun bootstrap after sync completes")
    collection = ensure(curated, "/api/collection", "Ledger Anomaly Analytics",
                        {"description": "Curated anomaly reports; source records are excluded."})
    dashboards, cards, dashboards_by_report = [], {}, {}
    specs = [("quality", "Data Quality", "findings_summary", "bar", None),
             ("status", "Pipeline Health", "pipeline_health", "table", None),
             ("churn", "Churn", "cohort_metrics", "bar", "churn"),
             ("revocation", "Revocation", "cohort_metrics", "bar", "revocation"),
             ("retention_24m", "Long-term Retention", "cohort_metrics", "bar", "retention_24m"),
             ("attendance", "Attendance", "cohort_metrics", "bar", "attendance"),
             ("model_quality", "Model Quality", "model_quality", "table", None)]
    for section, title in [('account_changes', 'Account Changes'), ('reset_activity', 'Reset Activity'),
                           ('reset_completions', 'Reset Completions'), ('registrations', 'Registration Patterns'),
                           ('email_similarity', 'Email Similarity'), ('bounce_outcomes', 'Bounce Outcomes'),
                           ('privilege_changes', 'Privilege Changes'), ('monitoring_coverage', 'Security Coverage')]:
        specs.append(('security_' + section, title, 'cohort_metrics', 'bar', 'security_' + section))
    for identifier, title, table_name, display, report in specs:
        table = tables[table_name]
        fields = {f["name"]: f["id"] for f in table.get("fields", [])}
        query = {"source-table": table["id"], "limit": 1000}
        if report:
            query["filter"] = ["=", ["field", fields["report"], None], report]
            query["aggregation"] = [["sum", ["field", fields["value"], None]]]
            query["breakout"] = [["field", fields["dimension"], None], ["field", fields["bucket"], None]]
        elif table_name == "findings_summary":
            query["aggregation"] = [["sum", ["field", fields["count"], None]]]
            query["breakout"] = [["field", fields["severity"], None]]
        card = ensure(curated, "/api/card", "Anomaly · " + title,
                      {"collection_id": collection["id"], "display": display, "visualization_settings": {},
                       "dataset_query": {"database": db_id, "type": "query", "query": query}})
        if (card.get("collection_id") != collection["id"]
                or card.get("dataset_query", {}).get("database") != db_id
                or not _notebook_query(card.get("dataset_query"))):
            raise MetabaseError("Existing report card is outside its expected curated collection or query scope")
        dashboard = ensure(curated, "/api/dashboard", "Anomaly · " + title,
                           {"collection_id": collection["id"], "description": "Curated, read-only analysis with coverage and sample-size context."})
        if dashboard.get("collection_id") != collection["id"]:
            raise MetabaseError("Existing report dashboard is outside its expected curated collection")
        current = curated._json("GET", f"/api/dashboard/{dashboard['id']}")
        if not current.get("dashcards"):
            curated._json("PUT", f"/api/dashboard/{dashboard['id']}", json={"dashcards": [
                {"id": -1, "card_id": card["id"], "row": 0, "col": 0, "size_x": 18, "size_y": 8,
                 "parameter_mappings": [], "series": []}]})
        dashboards.append(dashboard["id"])
        cards[identifier] = card["id"]
        dashboards_by_report[identifier] = dashboard["id"]
    raw_id = None
    if config.get("ANOMALY_METABASE_RAW_URL"):
        raw_key = config["ANOMALY_METABASE_RAW_API_KEY"]
        raw = MetabaseClient(config["ANOMALY_METABASE_RAW_URL"], raw_key, raw_key)
        raw_db = _ensure_raw_source(raw, resolved_mongo_uri(config["MLAB_URI"], config.get("MLAB_DATABASE")))
        raw_id = raw_db["id"]
    return {"reporting_database_id": db_id, "collection_id": collection["id"],
            "dashboard_ids": dashboards, "cards": cards,
            "dashboards_by_report": dashboards_by_report, "raw_database_id": raw_id}
