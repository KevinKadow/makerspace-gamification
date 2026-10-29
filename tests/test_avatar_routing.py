"""Exercise production claim construction, not just the in-memory queue."""
from unittest.mock import MagicMock

import pytest

from ledger.anomaly.slack import ANOMALY_KINDS
from ledger.cli import outbox_filters
from ledger.storage import MongoStore, MemoryStore, now
from ledger.worker import Worker


@pytest.mark.parametrize("kinds,exclude,expected", [
    (["avatar_generate"], ["anomaly_query"], {"$in": ["avatar_generate"], "$nin": ["anomaly_query"]}),
    (["avatar_generate"], ["avatar_generate"], {"$in": ["avatar_generate"], "$nin": ["avatar_generate"]}),
    (None, ["avatar_generate"], {"$nin": ["avatar_generate"]}),
    (["avatar_generate"], None, {"$in": ["avatar_generate"]}),
])
def test_mongo_claim_combines_constraints(kinds, exclude, expected):
    database = MagicMock()
    MongoStore(database).claim("ledger_outbox", kinds=kinds, exclude=exclude)
    query = database.__getitem__.return_value.find_one_and_update.call_args.args[0]
    assert query["kind"] == expected
    memory = MemoryStore()
    memory.put("ledger_outbox", {"_id": "avatar", "kind": "avatar_generate",
                               "status": "pending", "available_at": now()})
    claimed = memory.claim("ledger_outbox", kinds=kinds, exclude=exclude)
    allowed = (not kinds or "avatar_generate" in kinds) and (not exclude or "avatar_generate" not in exclude)
    assert bool(claimed) == allowed


@pytest.mark.parametrize("lane", ["results", "interactive", "avatars"])
def test_worker_uses_production_lane_and_anomaly_exclusions(lane):
    database = MagicMock()
    database.__getitem__.return_value.find_one_and_update.return_value = None
    worker = object.__new__(Worker)
    worker.store = MongoStore(database)
    assert not worker.step("ledger_outbox", **outbox_filters(lane))
    query = database.__getitem__.return_value.find_one_and_update.call_args.args[0]["kind"]
    assert query["$in"] == outbox_filters(lane)["kinds"]
    assert set(ANOMALY_KINDS) <= set(query["$nin"])
    if lane != "avatars":
        assert "avatar_generate" not in query["$in"]
        assert "avatar_reference" not in query["$in"]
