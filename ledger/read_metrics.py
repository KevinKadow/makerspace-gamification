"""Opt-in Mongo command counters; never retain filters or document bodies."""
from collections import defaultdict
import logging
from threading import Lock

from bson import BSON
from pymongo.monitoring import CommandListener


class ReadMetrics(CommandListener):
    def __init__(self):
        self.lock, self.pending = Lock(), {}
        self.totals = defaultdict(lambda: {"commands": 0, "documents": 0, "bytes": 0,
                                          "duration_us": 0, "failures": 0})

    def started(self, event):
        # Collection names and command names come from application/driver code.
        # No filter, URI, arguments, message text, or database name is retained.
        name = event.command_name
        collection = event.command.get(name) if name in ("find", "aggregate", "count", "distinct") else None
        if name == "getMore":
            collection = event.command.get("collection")
        label = name + (":" + collection if isinstance(collection, str) else "")
        with self.lock:
            self.pending[(event.connection_id, event.request_id)] = label

    def succeeded(self, event):
        cursor = event.reply.get("cursor", {})
        docs = cursor.get("firstBatch", cursor.get("nextBatch", []))
        try:
            size = len(BSON.encode(event.reply))
        except Exception:
            size = 0
        self._finish(event, len(docs), size, False)

    def failed(self, event):
        self._finish(event, 0, 0, True)

    def _finish(self, event, documents, size, failed):
        with self.lock:
            label = self.pending.pop((event.connection_id, event.request_id), event.command_name)
            total = self.totals[label]
            total["commands"] += 1
            total["documents"] += documents
            total["bytes"] += size
            total["duration_us"] += event.duration_micros
            total["failures"] += int(failed)

    def snapshot(self, reset=False):
        with self.lock:
            data = {key: dict(value) for key, value in self.totals.items()}
            if reset:
                self.totals.clear()
            return data


READ_METRICS = ReadMetrics()


def log_metrics():
    for operation, counts in READ_METRICS.snapshot(reset=True).items():
        logging.getLogger(__name__).info("Mongo operation=%s commands=%s documents=%s bytes=%s duration_us=%s failures=%s",
                                       operation, counts["commands"], counts["documents"], counts["bytes"],
                                       counts["duration_us"], counts["failures"])
