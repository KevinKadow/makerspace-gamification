import json
from threading import Event, Thread
import zipfile

import pytest

from ledger.slack_knowledge_import import (COLLECTION, ExportError, KnowledgeImporter,
                                          QwenDistiller, read_export)
from ledger.storage import MemoryStore


def archive(path, rows, extra=None):
    with zipfile.ZipFile(path, "w") as output:
        output.writestr("users.json", json.dumps([
            {"id": "U123", "profile": {"display_name": "Ada"}},
            {"id": "U456", "profile": {"display_name": "Bea"}}]))
        output.writestr("channels.json", json.dumps([
            {"id": "C123", "name": "workshop"},
            {"id": "G123", "name": "private", "is_private": True}]))
        output.writestr("workshop/2025-01-01.json", json.dumps(rows))
        if extra:
            output.writestr(extra, "{}")
    return path


def message(ts, text, user="U123", **extra):
    return {"type": "message", "ts": ts, "text": text, "user": user, **extra}


def summary(messages):
    return {"status": "OK", "topic": "Laser", "participants": ["Ada"],
            "problem_context": messages[0]["text"], "resolution_decision": "Repaired",
            "action_items": []}


class Index:
    def __init__(self):
        self.published, self.removed = [], []
        self.fail_publish = False
        self.fail_remove = False

    def publish(self, row):
        if self.fail_publish:
            raise OSError("offline")
        self.published.append(row["generation"])

    def remove(self, thread_id):
        if self.fail_remove:
            raise OSError("offline")
        self.removed.append(thread_id)


def test_reconstructs_threads_resolves_names_and_drops_noise(tmp_path):
    rows = [message("10.000001", "How to use <#C123> and <@U456>?"),
            message("11.000001", "Use the guide", "U456", thread_ts="10.000001"),
            message("12.000001", ":tada: 🙌"),
            message("13.000001", "joined", subtype="channel_join"),
            message("14.000001", "offline", bot_id="B123")]
    result = read_export(archive(tmp_path / "export.zip", rows), "T123")
    assert len(result) == 1
    assert result[0]["_id"] == "T123:C123:10.000001"
    assert result[0]["messages"][0]["text"] == "How to use #workshop and @Bea?"
    assert result[0]["messages"][1]["display_name"] == "Bea"


def test_archive_rejects_traversal_before_processing(tmp_path):
    with pytest.raises(ExportError):
        read_export(archive(tmp_path / "export.zip", [], "../evil.json"), "T123")


def test_incremental_import_waits_for_parent_and_only_redistills_changed_threads(tmp_path):
    store, index, calls = MemoryStore(), Index(), []
    importer = KnowledgeImporter(store, lambda rows: (calls.append(rows) or summary(rows)), index)
    reply = message("11.000001", "Resolved by cleaning", "U456", thread_ts="10.000001")
    assert importer.import_zip(archive(tmp_path / "a.zip", [reply]), "T123")["pending"] == 1
    assert not calls
    root = message("10.000001", "Why is it blocked?")
    assert importer.import_zip(archive(tmp_path / "b.zip", [root]), "T123")["ready"] == 1
    assert len(calls) == 1 and len(calls[0]) == 2
    assert importer.import_zip(archive(tmp_path / "c.zip", [root, reply]), "T123")["unchanged"] == 1
    assert len(calls) == 1
    added = message("12.000001", "Clean weekly", thread_ts="10.000001")
    assert importer.import_zip(archive(tmp_path / "d.zip", [added]), "T123")["ready"] == 1
    assert len(calls) == 2 and len(calls[1]) == 3


def test_screen_quarantines_without_persisting_or_sending_sensitive_text(tmp_path):
    store, index, calls = MemoryStore(), Index(), []
    importer = KnowledgeImporter(store, lambda rows: calls.append(rows), index)
    result = importer.import_zip(archive(tmp_path / "x.zip", [message("10.000001", "door code is 1234")]), "T123")
    assert result["quarantine"] == 1 and not calls
    record = store.get(COLLECTION, "T123:C123:10.000001")
    assert "1234" not in json.dumps(record, default=str)
    assert record["status"] == "quarantine"


def test_index_failure_retries_without_redistillation(tmp_path):
    store, index, calls = MemoryStore(), Index(), []
    importer = KnowledgeImporter(store, lambda rows: (calls.append(rows) or summary(rows)), index)
    path = archive(tmp_path / "x.zip", [message("10.000001", "How to repair?")])
    index.fail_publish = True
    assert importer.import_zip(path, "T123")["error"] == 1
    assert store.get(COLLECTION, "T123:C123:10.000001")["status"] == "ready"
    index.fail_publish = False
    assert importer.import_zip(path, "T123")["unchanged"] == 1
    assert len(calls) == 1 and len(index.published) == 1


def test_removed_thread_stays_removed_on_reimport(tmp_path):
    store, index = MemoryStore(), Index()
    importer = KnowledgeImporter(store, summary, index)
    path = archive(tmp_path / "x.zip", [message("10.000001", "How to repair?")])
    importer.import_zip(path, "T123")
    assert importer.remove("T123:C123:10.000001")
    assert importer.import_zip(path, "T123")["removed"] == 1
    assert store.get(COLLECTION, "T123:C123:10.000001")["status"] == "removed"


def test_removed_thread_retries_failed_vector_cleanup(tmp_path):
    store, index = MemoryStore(), Index()
    importer = KnowledgeImporter(store, summary, index)
    path = archive(tmp_path / "x.zip", [message("10.000001", "How to repair?")])
    importer.import_zip(path, "T123")
    index.fail_remove = True
    with pytest.raises(OSError):
        importer.remove("T123:C123:10.000001")
    assert store.get(COLLECTION, "T123:C123:10.000001")["status"] == "removed"
    index.fail_remove = False
    assert importer.import_zip(path, "T123")["removed"] == 1
    assert index.removed[-1] == "T123:C123:10.000001"


def test_overlapping_import_and_removal_are_serialized(tmp_path):
    store, index, entered, release = MemoryStore(), Index(), Event(), Event()
    def blocking(rows):
        entered.set()
        assert release.wait(5)
        return summary(rows)
    importer = KnowledgeImporter(store, blocking, index)
    path = archive(tmp_path / "x.zip", [message("10.000001", "How to repair?")])
    results = []
    worker = Thread(target=lambda: results.append(importer.import_zip(path, "T123")))
    worker.start()
    assert entered.wait(5)
    assert importer.import_zip(path, "T123")["busy"] == 1
    with pytest.raises(RuntimeError, match="being processed"):
        importer.remove("T123:C123:10.000001")
    release.set()
    worker.join(5)
    assert results[0]["ready"] == 1
    assert store.get(COLLECTION, "T123:C123:10.000001")["status"] == "ready"


def test_display_name_change_at_same_revision_is_not_a_content_conflict(tmp_path):
    store, index = MemoryStore(), Index()
    importer = KnowledgeImporter(store, summary, index)
    path = archive(tmp_path / "x.zip", [message("10.000001", "How to repair?")])
    original = read_export(path, "T123")[0]
    assert importer.import_thread(original) == "ready"
    renamed = dict(original)
    renamed["messages"] = [dict(original["messages"][0], display_name="New name")]
    assert importer.import_thread(renamed) == "ready"
    assert store.get(COLLECTION, original["_id"])["status"] == "ready"


def test_invalid_model_output_is_retryable_and_skip_is_stable(tmp_path):
    store, index, outputs = MemoryStore(), Index(), [{"broken": True}]
    def distill(rows):
        return outputs[0]
    importer = KnowledgeImporter(store, distill, index)
    path = archive(tmp_path / "x.zip", [message("10.000001", "Any updates?")])
    assert importer.import_zip(path, "T123")["error"] == 1
    assert store.get(COLLECTION, "T123:C123:10.000001")["status"] == "pending"
    outputs[0] = {"status": "SKIP", "topic": "", "participants": [], "problem_context": "",
                  "resolution_decision": "", "action_items": []}
    assert importer.import_zip(path, "T123")["skip"] == 1
    assert importer.import_zip(path, "T123")["unchanged"] == 1


def test_distiller_checks_rendered_tokens_and_disables_thinking_via_adapter():
    class API:
        def tokenize(self, messages):
            return len(messages[-1]["content"])

        def _request(self, messages, temperature, max_tokens, **kwargs):
            assert kwargs["response_format"]["type"] == "json_schema"
            return json.dumps(summary([{"text": "Question"}]))

    distill = QwenDistiller(API(), context_limit=3000, output_tokens=2000)
    result = distill([{"ts": "10.000001", "display_name": "Ada", "text": "Question"}])
    assert result["status"] == "OK"
    assert distill([{"ts": "10.000001", "display_name": "Ada", "text": "x" * 1000}])["status"] == "OK"
    with pytest.raises(ValueError, match="context limit"):
        QwenDistiller(API(), context_limit=10, output_tokens=2000)(
            [{"ts": "10.000001", "display_name": "Ada", "text": "x"}])
