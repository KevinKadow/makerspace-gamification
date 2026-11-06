from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from bson import ObjectId

from ledger.domain import Ledger
from ledger.messages import Composer
from ledger.sources import FIELDS, MemorySources
from ledger.storage import MemoryStore, now


def oid(n):
    return ObjectId(f"{n:024x}")


@pytest.fixture
def env():
    data = {name: [] for name in FIELDS}
    for i in range(1, 12):
        data["members"].append({"_id": oid(i), "firstname": f"Maker{i}", "lastname": "Test",
            "status": "activeMember", "role": "admin" if i == 10 else "board_member" if i == 11 else "member",
            "expirationTime": int((now() + timedelta(days=60)).timestamp() * 1000), "subscription": True})
        data["slack_users"].append({"_id": oid(100 + i), "member_id": oid(i), "slack_id": f"U{i}"})
    for i in range(1, 6):
        data["shops"].append({"_id": oid(200 + i), "name": f"Shop{i}"})
        for j in range(1, 5):
            data["tools"].append({"_id": oid(300 + i * 10 + j), "shop_id": oid(200 + i), "name": f"Tool{i}-{j}",
                "prerequisite_ids": [] if j == 1 else [oid(300 + i * 10 + 1)]})
    store, source = MemoryStore(), MemorySources(data)
    ledger = Ledger(store, source)
    ledger.seed()
    store.put("ledger_channels", {"_id": "chat", "kind": "channel", "channel_id": "CCHAT", "slot": 0})
    for i in range(1, 7):
        store.put("ledger_channels", {"_id": f"rank:{i}", "kind": "channel", "channel_id": f"CRANK{i}", "slot": i})
    api = MagicMock()
    api.complete.return_value = "The Ledger recognizes this contribution."
    slack = MagicMock()
    slack.users_info.side_effect = lambda user: {"user": {"id": user, "deleted": False, "is_bot": False}}
    slack.conversations_open.side_effect = lambda users: {"channel": {"id": "D" + users}}
    slack.chat_postMessage.return_value = {"ts": "123.456"}
    slack.files_upload_v2.return_value = {"files": [{"id": "F_RANK_ICON"}]}
    slack.conversations_members.return_value = {"members": [], "response_metadata": {}}
    slack.conversations_info.return_value = {"channel": {"is_member": True}}
    slack.chat_getPermalink.return_value = {"permalink": "https://example.slack.com/archives/thread"}
    return ledger, store, source, Composer(store, api), api, slack


@pytest.fixture
def joined(env):
    ledger, *_ = env
    ledger.join(str(oid(1)))
    ledger.join(str(oid(2)))
    return env
