"""Queue startup must not depend on a reachable MQTT broker."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ledger.cli import RECONCILE_INTERVAL_SECONDS, broker, periodic_reconcile_key
from ledger.sources import FIELDS
from ledger.storage import MemoryStore


@pytest.mark.parametrize('subscribe', [True, False])
def test_broker_connects_in_background_and_resubscribes(subscribe, caplog):
    client = MagicMock()
    client.connect.side_effect = ConnectionRefusedError('private connection details')
    with patch('ledger.cli.mqtt.Client', return_value=client), patch.dict('os.environ', {'MQTT_HOST': 'offline-broker'}, clear=True):
        assert broker(MemoryStore(), subscribe=subscribe) is client
    client.connect.assert_not_called()
    client.connect_async.assert_called_once_with('offline-broker', 1883, 60)
    client.loop_start.assert_called_once()
    client.reconnect_delay_set.assert_called_once_with(min_delay=1, max_delay=60)
    client.on_connect_fail(client, None)
    assert 'while Slack queues continue' in caplog.text
    assert 'private connection details' not in caplog.text
    failed = MagicMock(is_failure=True)
    client.on_connect(client, None, None, failed, None)
    client.subscribe.assert_not_called()
    for _ in range(2):
        client.on_connect(client, None, None, MagicMock(is_failure=False), None)
    if subscribe:
        assert client.subscribe.call_count == 2
        assert client.subscribe.call_args.args[0] == [(f'{name}/+', 1) for name in FIELDS if name not in ('checkins', 'cards')] + [('checkins/insert', 1)]
    else:
        client.subscribe.assert_not_called()


def test_broker_uses_process_name_and_random_suffix_when_client_id_is_unset():
    client = MagicMock()
    with (patch('ledger.cli.mqtt.Client', return_value=client) as constructor,
          patch('ledger.cli.current_process', return_value=SimpleNamespace(name='Ledger Accounting')),
          patch('ledger.cli.secrets.randbelow', return_value=42),
          patch.dict('os.environ', {'MQTT_HOST': 'broker'}, clear=True)):
        broker(MemoryStore())

    assert constructor.call_args.kwargs['client_id'] == 'Ledger-Accounting-000042'


def test_broker_preserves_configured_client_id():
    client = MagicMock()
    with (patch('ledger.cli.mqtt.Client', return_value=client) as constructor,
          patch.dict('os.environ', {'MQTT_HOST': 'broker', 'MQTT_CLIENT_ID': 'configured-ledger'}, clear=True)):
        broker(MemoryStore())

    assert constructor.call_args.kwargs['client_id'] == 'configured-ledger'


def test_periodic_reconciliation_uses_thirteen_minute_buckets():
    assert RECONCILE_INTERVAL_SECONDS == 13 * 60
    assert periodic_reconcile_key(0) == periodic_reconcile_key(779)
    assert periodic_reconcile_key(780) != periodic_reconcile_key(779)
