from unittest.mock import MagicMock, patch

import pytest
from pymongo import MongoClient

from ledger.cli import dependencies, make_app
from ledger.http import HTTPApp
from ledger.storage import connect_database


@pytest.mark.parametrize("settings,source_uri,writer_uri,source_db,writer_db", [
    ({"MLAB_URI": "mongodb://reader/source", "LEDGER_URI": "mongodb://writer/game"},
     "mongodb://reader/source", "mongodb://writer/game", None, None),
    ({"MLAB_URI": "mongodb://reader/source", "LEDGER_URI": "mongodb://writer/game",
      "MLAB_DATABASE": "source_override", "LEDGER_DATABASE": "game_override", "MONGO_URI": "mongodb://old", "MONGO_DATABASE": "old_db"},
     "mongodb://reader/source", "mongodb://writer/game", "source_override", "game_override"),
    ({"MONGO_URI": "mongodb://old", "MONGO_DATABASE": "old_db"},
     "mongodb://old", "mongodb://old", "old_db", "old_db"),
    ({"MLAB_URI": "mongodb://reader/source", "MONGO_URI": "mongodb://old"},
     "mongodb://reader/source", "mongodb://old", None, None),
    ({"LEDGER_URI": "mongodb://writer/game", "MONGO_URI": "mongodb://old"},
     "mongodb://old", "mongodb://writer/game", None, None),
])
def test_connections_route_source_reads_and_ledger_writes_separately(settings, source_uri, writer_uri, source_db, writer_db):
    from ledger.storage import MongoStore
    reader_database, writer_database = MagicMock(), MagicMock()
    store = MongoStore(writer_database)
    with patch.dict("os.environ", {**settings, "SLACK_BOT_TOKEN": "xoxb-test"}, clear=True), \
         patch("ledger.cli.connect", return_value=store) as writer, \
         patch("ledger.cli.connect_database", return_value=reader_database) as reader:
        ledger, composer, client = dependencies()
    writer.assert_called_once_with(writer_uri, writer_db)
    reader.assert_called_once_with(source_uri, source_db)
    assert composer.api.url == "http://localhost:8000/v1"
    assert composer.api.model == "nvidia/Qwen3.8-27B-NVFP4"
    ledger.sources.rows("members")
    reader_database.__getitem__.assert_called_once_with("members")
    writer_database.__getitem__.assert_not_called()
    ledger.store.put("ledger_participants", {"_id": "test"})
    writer_database.__getitem__.assert_called_once_with("ledger_participants")
    reader_database.__getitem__.assert_called_once_with("members")


def test_vllm_endpoint_model_alias_and_key_override():
    settings = {"MLAB_URI": "mongodb://reader/source", "LEDGER_URI": "mongodb://writer/game",
                "SLACK_BOT_TOKEN": "xoxb-test", "LEDGER_LLM_BASE_URL": "http://ledger-ai:8000/v1",
                "LEDGER_LLM_MODEL": "ledger-qwen", "LEDGER_LLM_API_KEY": "test-key"}
    with patch.dict("os.environ", settings, clear=True), patch("ledger.cli.connect"), patch("ledger.cli.connect_database"):
        _, composer, _ = dependencies()
    assert composer.api.url == settings["LEDGER_LLM_BASE_URL"]
    assert composer.api.model == "ledger-qwen" and composer.api.api_key == "test-key"


@pytest.mark.parametrize("settings", [{}, {"MLAB_URI": "mongodb://reader"}, {"LEDGER_URI": "mongodb://writer"}])
def test_missing_uri_fails_without_reusing_the_other_credential(settings):
    with patch.dict("os.environ", settings, clear=True), patch("ledger.cli.connect") as connect:
        with pytest.raises(ValueError, match="MLAB_URI and LEDGER_URI"):
            dependencies()
    connect.assert_not_called()


@pytest.mark.parametrize("uri,override,expected", [
    ("mongodb://localhost/source", None, "source"),
    ("mongodb://localhost/game?authSource=admin", None, "game"),
    ("mongodb://localhost", None, "makerauth"),
    ("mongodb://localhost/source", "override", "override"),
])
def test_database_selection_and_independent_clients_without_network(uri, override, expected):
    def disconnected(*args, **kwargs):
        return MongoClient(*args, **kwargs, connect=False)
    with patch("ledger.storage.MongoClient", side_effect=disconnected):
        first = connect_database(uri, override)
        second = connect_database(uri, override)
    try:
        assert first.name == second.name == expected
        assert first.client is not second.client
    finally:
        first.client.close()
        second.client.close()


@pytest.mark.parametrize("failure", [None, "source", "ledger"])
def test_readiness_checks_both_connections_and_hides_connection_errors(failure):
    source, ledger = MagicMock(), MagicMock()
    if failure:
        (source if failure == "source" else ledger).ready.side_effect = RuntimeError("credentials must not escape")
    app = HTTPApp(MagicMock(), ledger, source)
    statuses = []
    body = b"".join(app({"PATH_INFO": "/ready", "REQUEST_METHOD": "GET"}, lambda status, headers: statuses.append(status)))
    assert statuses[0].startswith("503" if failure else "200")
    assert b"credentials" not in body
    ledger.ready.assert_called_once()
    if failure != "ledger":
        source.ready.assert_called_once()


def test_http_factory_wires_the_source_health_check():
    ledger = MagicMock()
    with patch("ledger.cli.dependencies", return_value=(ledger, MagicMock(), MagicMock())), \
         patch("ledger.cli.build_app"), patch.dict("os.environ", {"SLACK_BOT_TOKEN": "test", "SLACK_SIGNING_SECRET": "test", "SLACK_TEAM_ID": "T1", "SLACK_BOT_USER_ID": "U1"}):
        app = make_app()
    assert app.sources is ledger.sources and app.store is ledger.store
