"""Native Metabot transport, ownership and bounded chart delivery contracts."""
import json
from copy import deepcopy

import pytest
import requests

from ledger.anomaly.metabase import MetabaseClient, MetabaseError, bootstrap, resolved_mongo_uri


CONVERSATION = "bd65f0ab-eed1-41ae-b360-4d61efcf3fa6"
PARENT = "a7d4914e-8042-4d05-b83b-2c53b4a93d1a"
CHART = "93ef6d50-8950-411e-909f-36f6868aa7a9"
PNG = b"\x89PNG\r\n\x1a\nexample"


class Response:
    def __init__(self, status=200, *, data=None, events=(), content=PNG, content_type=None):
        self.status_code = status
        self.data = data
        self.events = events
        self.content = content
        self.headers = {"Content-Type": content_type or "text/event-stream"}
        self.closed = False

    def json(self):
        return self.data

    def close(self):
        self.closed = True

    def iter_lines(self, **_):
        for event in self.events:
            yield "data: " + (event if isinstance(event, str) else json.dumps(event))

    def iter_content(self, size):
        for start in range(0, len(self.content), size):
            yield self.content[start:start + size]


class Session:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def client(*responses):
    session = Session(*responses)
    return MetabaseClient("https://curated.example", "chat-secret", "chart-secret", session=session), session


def test_native_turn_ignores_model_query_and_binds_chart_to_its_conversation():
    stream = Response(events=[
        {"type": "text-delta", "delta": "Attendance declined."},
        {"type": "data-generated_entity", "data": {"type": "card", "id": CHART,
            "query": {"native": "DO NOT EXECUTE"}, "conversation_id": PARENT}},
        {"type": "data-static_viz", "data": {"entity_id": 42}},
        {"type": "finish", "finishReason": "stop"}, "[DONE]",
    ])
    mb, session = client(Response(404), stream)
    result = mb.ask("What changed?", CONVERSATION, {"default_database_id": 7})
    assert result["answer"] == "Attendance declined."
    assert result["chartrefs"] == [
        {"type": "conversation_chart", "conversation_id": CONVERSATION, "chart_id": CHART},
        {"type": "saved_card", "card_id": 42},
    ]
    payload = session.calls[-1][2]["json"]
    assert payload["profile_id"] == "anomaly-analytics"
    assert payload["conversation_id"] == CONVERSATION
    assert "parent_message_id" not in payload
    assert all(call[2]["allow_redirects"] is False for call in session.calls)
    assert stream.closed


def test_restart_recovers_parent_from_persisted_conversation():
    previous = Response(data={"messages": [
        {"id": "123", "externalId": PARENT, "role": "agent", "status": {"type": "done"}},
    ]})
    mb, session = client(previous, Response(events=[{"type": "finish", "finishReason": "stop"}]))
    mb.ask("Compare with last month", CONVERSATION)
    assert session.calls[0][1].endswith("/conversations/" + CONVERSATION)
    assert session.calls[1][2]["json"]["parent_message_id"] == PARENT


def test_inflight_turn_and_forbidden_history_never_submit_another_turn():
    for response in [Response(data={"messages": [{"role": "agent", "status": {"type": "in_progress"}}]}),
                     Response(403, data={"error": "secret"})]:
        mb, session = client(response)
        with pytest.raises(MetabaseError) as caught:
            mb.ask("Question", CONVERSATION)
        assert len(session.calls) == 1
        assert "secret" not in str(caught.value)


@pytest.mark.parametrize("events", [
    [{"type": "text-delta", "delta": "unfinished"}, "[DONE]"],
    [{"type": "finish", "finishReason": "length"}],
    [{"type": "error", "errorText": "secret detail"}],
    ["invalid json"],
])
def test_stream_errors_do_not_return_partial_answers_or_retry(events):
    stream = Response(events=events)
    mb, session = client(Response(404), stream)
    with pytest.raises(MetabaseError) as caught:
        mb.ask("Question", CONVERSATION)
    assert "secret detail" not in str(caught.value)
    assert len(session.calls) == 2
    assert stream.closed


@pytest.mark.parametrize("reference", [
    {"url": "https://elsewhere.example/private"},
    {"type": "saved_card", "card_id": 1, "query": {}},
    {"type": "saved_card", "card_id": True},
    {"type": "conversation_chart", "conversation_id": "../other", "chart_id": CHART},
])
def test_render_never_accepts_arbitrary_urls_queries_or_malformed_refs(reference):
    mb, session = client()
    with pytest.raises(ValueError):
        mb.render(reference)
    assert not session.calls


def test_render_uses_conversation_owner_key_and_separate_saved_card_key():
    mb, session = client(Response(content_type="image/png"), Response(content_type="image/png"))
    assert mb.render({"type": "conversation_chart", "conversation_id": CONVERSATION, "chart_id": CHART}) == PNG
    assert mb.render({"type": "saved_card", "card_id": 42}) == PNG
    assert [call[2]["headers"]["X-API-Key"] for call in session.calls] == ["chat-secret", "chart-secret"]
    assert {call[1] for call in session.calls} == {"https://curated.example/api/metabot/analytics/render"}


@pytest.mark.parametrize("response", [Response(302), Response(content_type="text/html"),
    Response(content_type="image/png", content=b"not png"),
    Response(content_type="image/png", content=PNG + b"x" * (8 * 1024 * 1024))])
def test_render_rejects_redirects_non_png_and_oversized_images(response):
    mb, _ = client(response)
    with pytest.raises(MetabaseError):
        mb.render({"type": "saved_card", "card_id": 1})
    assert response.closed


def test_network_error_does_not_include_credentials_or_payload():
    mb, _ = client(requests.ConnectionError("https://chat-secret@private.example/?query=private"))
    with pytest.raises(MetabaseError, match="^Metabase is unavailable$"):
        mb.ask("Question", CONVERSATION)


def test_setting_update_accepts_native_no_content_response():
    mb, _ = client(Response(204))
    assert mb._json("PUT", "/api/setting/anomaly-analytics-only", json={"value": True}) is None


def test_mongo_database_override_preserves_authsource_and_escaped_password():
    assert resolved_mongo_uri("mongodb+srv://reader:p%40ss@mongo.example/old?authSource=admin", "new") == (
        "mongodb+srv://reader:p%40ss@mongo.example/new?authSource=admin")
    assert resolved_mongo_uri("mongodb://mongo.example") == "mongodb://mongo.example/makerauth"
    with pytest.raises(ValueError):
        resolved_mongo_uri("https://elsewhere.example/data")


@pytest.fixture
def bootstrap_setup(monkeypatch):
    from ledger.anomaly import metabase
    names = {"cohort_metrics": ["report", "dimension", "bucket", "value", "sample_size", "as_of"],
             "findings_summary": ["collection", "rule", "severity", "count", "first_seen", "last_seen"],
             "pipeline_health": ["component", "status", "updated_at"],
             "model_quality": ["outcome", "model_version", "metric", "value", "sample_size", "as_of"]}
    tables = [{"id": i, "name": name, "schema": "reporting", "fields": [
        {"id": i * 100 + j, "name": field} for j, field in enumerate(fields)]}
        for i, (name, fields) in enumerate(names.items(), 1)]

    class SetupClient:
        objects = {"/api/database": [], "/api/collection": [], "/api/card": [], "/api/dashboard": []}
        raw_objects = {"/api/database": []}
        settings = {}
        updates = []
        card_payloads = []
        calls = []
        modern = False
        mask_uri = False
        fail_raw_update = False

        def __init__(self, base_url, *_):
            self.raw = base_url == "https://raw.example"
            if self.raw:
                self.objects = self.raw_objects

        def _json(self, method, path, *, json=None, **_):
            self.calls.append((self.raw, method, path, deepcopy(json)))
            value = deepcopy(self.response(method, path, json=json))
            if self.raw and self.mask_uri:
                for row in value if isinstance(value, list) else [value]:
                    if isinstance(row, dict) and row.get("details", {}).get("conn-uri"):
                        row["details"]["conn-uri"] = "**MetabasePass**"
            return value

        def response(self, method, path, *, json=None):
            if self.raw and method == "PUT" and path.startswith("/api/database/"):
                if self.fail_raw_update:
                    raise MetabaseError("Metabase request failed (HTTP 400)")
                database = next(row for row in self.objects["/api/database"] if path.endswith('/' + str(row['id'])))
                database["details"].update(json["details"])
                return database
            if path.endswith("/metadata"):
                return {"tables": tables}
            if path.startswith("/api/setting/"):
                self.settings[path] = json["value"]
                return {}
            if path in self.objects:
                if method == "GET":
                    return self.objects[path]
                if path == "/api/collection":
                    # This fork's collection POST schema is closed; color is not accepted.
                    assert set(json) <= {"name", "description", "parent_id", "namespace", "authority_level"}
                if path == "/api/card":
                    self.card_payloads.append(json)
                value = {"id": 10 + len(self.objects[path]), **json}
                if path == "/api/card" and self.modern:
                    query = value["dataset_query"]
                    value["dataset_query"] = {"lib/type": "mbql/query", "database": query["database"],
                                              "stages": [{"lib/type": "mbql.stage/mbql", **query["query"]}]}
                self.objects[path].append(value)
                return value
            if path.startswith("/api/dashboard/"):
                if method == "GET":
                    return {"dashcards": [1] if path in self.updates else []}
                self.updates.append(path)
                return {}
            raise AssertionError(path)

    monkeypatch.setattr(metabase, "MetabaseClient", SetupClient)
    config = {"ANOMALY_METABASE_URL": "https://curated.example", "ANOMALY_METABASE_BOOTSTRAP_API_KEY": "setup",
              "ANOMALY_REPORTING_DB_HOST": "postgres", "ANOMALY_REPORTING_DB_USER": "reader",
              "ANOMALY_REPORTING_DB_PASSWORD": "secret"}
    return SetupClient, config


@pytest.mark.parametrize("modern", [False, True])
def test_bootstrap_creates_curated_views_and_dashboards_without_native_queries(bootstrap_setup, modern):
    SetupClient, config = bootstrap_setup
    SetupClient.modern = modern
    result = bootstrap(config)
    assert len(result["dashboard_ids"]) == 15
    assert set(result["cards"]) == {"quality", "status", "churn", "revocation", "retention_24m", "attendance", "model_quality",
        'security_account_changes', 'security_reset_activity', 'security_reset_completions', 'security_registrations',
        'security_email_similarity', 'security_bounce_outcomes', 'security_privilege_changes', 'security_monitoring_coverage'}
    assert set(result["cards"]) == set(result["dashboards_by_report"])
    assert bootstrap(config) == result
    assert len(SetupClient.objects["/api/card"]) == 15
    assert len(SetupClient.updates) == 15
    assert SetupClient.settings["/api/setting/anomaly-analytics-only"] is True
    assert SetupClient.settings["/api/setting/anomaly-analytics-database-ids"] == [10]
    for card in SetupClient.card_payloads:
        assert card["dataset_query"]["type"] == "query"
        assert card["dataset_query"]["database"] == 10
        assert card["dataset_query"]["query"]["source-table"] in {1, 2, 3, 4}
    assert "secret" not in str(result)


@pytest.mark.parametrize("changed", [
    {"host": "other"}, {"dbname": "other"}, {"user": "writer"}, {"port": 6432},
    {"ssl": True}, {"schema-filters-type": "exclusion"}, {"schema-filters-patterns": "public"},
    {"ssl-mode": "prefer"}, {"additional-options": "sslmode=disable"},
    {"sslmode": "disable"}, {"tunnel-enabled": True}, {"tunnel-enabled": 0},
])
def test_bootstrap_rejects_reporting_connection_drift_before_writes(bootstrap_setup, changed):
    setup, config = bootstrap_setup
    bootstrap(config)
    setup.objects["/api/database"][0]["details"].update(changed)
    setup.calls.clear()
    with pytest.raises(MetabaseError, match="reporting source does not match"):
        bootstrap(config)
    assert all(method == "GET" for _, method, _, _ in setup.calls)


@pytest.mark.parametrize("setting,value", [("ANOMALY_REPORTING_DB_PORT", "6432"), ("ANOMALY_REPORTING_DB_SSL", "true")])
def test_reporting_config_changes_require_connection_correction(bootstrap_setup, setting, value):
    _, config = bootstrap_setup
    bootstrap(config)
    with pytest.raises(MetabaseError, match="reporting source does not match"):
        bootstrap({**config, setting: value})


def test_reporting_accepts_equivalent_port_and_masked_password(bootstrap_setup):
    setup, config = bootstrap_setup
    config.update(ANOMALY_REPORTING_DB_PORT="6432", ANOMALY_REPORTING_DB_SSL="true")
    result = bootstrap(config)
    setup.objects["/api/database"][0]["details"].update(port="6432", password="**MetabasePass**", **{"ssl-mode": "require"})
    assert bootstrap(config) == result


@pytest.fixture
def raw_bootstrap_setup(bootstrap_setup):
    setup, config = bootstrap_setup
    config.update(ANOMALY_METABASE_RAW_URL="https://raw.example", ANOMALY_METABASE_RAW_API_KEY="raw-secret",
                  MLAB_URI="mongodb+srv://reader:p%40ss@mongo.example/old?authSource=admin&tls=true", MLAB_DATABASE="source")
    return setup, config


@pytest.mark.parametrize("changed", [
    {"engine": "postgres"}, {"details": {}},
    {"details": {"use-conn-uri": False}},
    {"details": {"conn-uri": "mongodb://writer:private-password@other.example/wrong"}},
    {"details": {"conn-uri": "mongodb+srv://writer:private-password@mongo.example/source?authSource=admin&tls=true"}},
    {"details": {"conn-uri": "mongodb+srv://reader:p%40ss@mongo.example/source?authSource=admin&tls=false"}},
    {"details": {"dbname": "other"}}, {"details": {"tunnel-enabled": True}},
])
def test_bootstrap_rejects_reused_raw_connection_mismatch(raw_bootstrap_setup, changed):
    setup, config = raw_bootstrap_setup
    bootstrap(config)
    database = setup.raw_objects["/api/database"][0]
    if changed.get("details"):
        database["details"].update(changed["details"])
    else:
        database.update(changed)
    setup.calls.clear()
    with pytest.raises(MetabaseError, match="raw source does not match") as caught:
        bootstrap(config)
    assert "private-password" not in str(caught.value) and "p%40ss" not in str(caught.value)
    assert all(method == "GET" for raw, method, _, _ in setup.calls if raw)


@pytest.mark.parametrize("masked", [False, True])
def test_raw_bootstrap_reuses_resolved_source_with_native_uri_masking(raw_bootstrap_setup, masked):
    setup, config = raw_bootstrap_setup
    setup.mask_uri = masked
    result = bootstrap(config)
    # Native creation may add ssl=true; URI mode ignores this separate flag.
    setup.raw_objects["/api/database"][0]["details"]["ssl"] = True
    setup.calls.clear()
    assert bootstrap(config) == result
    assert len(setup.raw_objects["/api/database"]) == 1
    updates = [payload for raw, method, _, payload in setup.calls if raw and method == "PUT"]
    assert len(updates) == int(masked)
    if masked:
        assert updates[0]["details"]["conn-uri"] == resolved_mongo_uri(config["MLAB_URI"], "source")
    assert "p%40ss" not in str(result) and "raw-secret" not in str(result)


def test_masked_raw_source_is_reestablished_from_configuration(raw_bootstrap_setup):
    setup, config = raw_bootstrap_setup
    result = bootstrap(config)
    setup.mask_uri = True
    database = setup.raw_objects["/api/database"][0]
    database["details"]["conn-uri"] = "mongodb://writer:secret@other.example/wrong"
    assert bootstrap(config) == result
    assert database["details"]["conn-uri"] == resolved_mongo_uri(config["MLAB_URI"], "source")
    setup.fail_raw_update = True
    with pytest.raises(MetabaseError, match="HTTP 400"):
        bootstrap(config)


def test_bootstrap_refuses_another_source_before_modifying_instance(monkeypatch):
    from ledger.anomaly import metabase

    class OtherSource:
        def __init__(self, *_):
            pass

        def _json(self, method, path):
            assert (method, path) == ("GET", "/api/database")
            return {"data": [{"id": 1, "name": "Raw Source"}]}

    monkeypatch.setattr(metabase, "MetabaseClient", OtherSource)
    with pytest.raises(MetabaseError, match="dedicated reporting-only instance"):
        bootstrap({"ANOMALY_METABASE_URL": "https://curated.example", "ANOMALY_METABASE_BOOTSTRAP_API_KEY": "setup"})
