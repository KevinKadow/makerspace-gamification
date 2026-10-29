from unittest.mock import Mock

import pytest

from ledger.anomaly.slack import AnomalyAccess, queue_anomaly_request, route_anomaly_event
from ledger.cli import outbox_filters
from ledger.domain import Denied
from ledger.slack_app import SlackUI
from ledger.worker import Worker


@pytest.fixture
def anomaly_env(env, monkeypatch):
    monkeypatch.setenv('ANOMALY_ENABLED', 'true')
    monkeypatch.setenv('SLACK_CHANNEL_ANOMALY', 'CAUDIT')
    ledger, store, source, composer, api, slack = env
    def info(channel):
        if channel.startswith('D'):
            return {'channel': {'id': channel, 'is_im': True, 'user': channel[1:]}}
        return {'channel': {'id': channel, 'name': 'ledger-audit', 'is_member': True,
                            'is_archived': False, 'is_ext_shared': False}}
    slack.conversations_info.side_effect = info
    slack.conversations_members.return_value = {'members': ['U1', 'U2', 'UANY', 'U10', 'U11'], 'response_metadata': {}}
    return env


@pytest.mark.parametrize('user', ['U10', 'U11'])
@pytest.mark.parametrize('status', ['activeMember', 'expired'])
def test_staff_detail_uses_current_source_role_without_game_consent(anomaly_env, monkeypatch, user, status):
    ledger, store, source, _, _, slack = anomaly_env
    source.data['members'][int(user[1:]) - 1]['status'] = status
    monkeypatch.setenv('SLACKIDS_INELIGIBLE', '["U10", "U11"]')
    access = AnomalyAccess(ledger, slack)
    assert access.staff(user)
    slack.conversations_members.return_value = {'members': [], 'response_metadata': {}}
    assert access.authorize(user, 'D' + user) == 'staff_detail'
    slack.conversations_members.return_value = {'members': [user], 'response_metadata': {}}
    assert not store.select('ledger_participants')
    # Source permission changes revoke staff detail even in a reused instance.
    source.data['members'][int(user[1:]) - 1]['role'] = 'member'
    assert not access.staff(user)
    assert access.authorize(user, 'D' + user) == 'aggregate'


@pytest.mark.parametrize('user', ['U1', 'UANY', 'U10'])
def test_shared_channel_is_aggregate_even_for_staff_and_unlinked_humans(anomaly_env, user):
    ledger, store, _, _, _, slack = anomaly_env
    assert AnomalyAccess(ledger, slack).authorize(user, 'CAUDIT') == 'aggregate'
    assert not store.select('ledger_participants')
    slack.conversations_invite.assert_not_called()
    slack.conversations_join.assert_not_called()


def test_current_channel_members_can_ask_in_own_dm(anomaly_env):
    ledger, _, _, _, _, slack = anomaly_env
    access = AnomalyAccess(ledger, slack)
    assert access.authorize('UANY', 'DUANY') == 'aggregate'
    slack.conversations_members.return_value = {'members': [], 'response_metadata': {}}
    with pytest.raises(Denied):
        access.authorize('UANY', 'DUANY')


@pytest.mark.parametrize('destination', ['DU2', 'GOTHER', 'COTHER', 'not-a-channel'])
def test_rejects_other_users_dm_and_unapproved_destinations(anomaly_env, destination):
    ledger, _, _, _, _, slack = anomaly_env
    with pytest.raises(Denied):
        AnomalyAccess(ledger, slack).authorize('U1', destination)


@pytest.mark.parametrize('changes', [{'deleted': True}, {'is_bot': True}, {'is_app_user': True}, {'id': 'U2'}, {'deleted': None}])
def test_inactive_bot_and_unknown_human_identity_fail_closed(anomaly_env, changes):
    ledger, _, _, _, _, slack = anomaly_env
    slack.users_info.side_effect = None
    slack.users_info.return_value = {'user': {'id': 'U1', 'deleted': False, 'is_bot': False, **changes}}
    with pytest.raises(Denied):
        AnomalyAccess(ledger, slack).authorize('U1', 'CAUDIT')


@pytest.mark.parametrize('status', ['suspended', 'revoked', 'unknown', None])
def test_invalid_source_membership_cannot_grant_staff_detail(anomaly_env, status):
    ledger, _, source, _, _, slack = anomaly_env
    source.data['members'][9]['status'] = status
    assert not AnomalyAccess(ledger, slack).staff('U10')


def test_ambiguous_source_link_cannot_grant_staff_detail(anomaly_env):
    ledger, _, source, _, _, slack = anomaly_env
    source.data['slack_users'].append(dict(source.data['slack_users'][9]))
    assert not AnomalyAccess(ledger, slack).staff('U10')


def test_name_resolution_and_channel_membership_paginate(anomaly_env, monkeypatch):
    ledger, _, _, _, _, slack = anomaly_env
    monkeypatch.setenv('SLACK_CHANNEL_ANOMALY', '#ledger-audit')
    slack.conversations_list.side_effect = [
        {'channels': [{'id': 'COTHER', 'name': 'other'}], 'response_metadata': {'next_cursor': 'next'}},
        {'channels': [{'id': 'CAUDIT', 'name': 'ledger-audit'}], 'response_metadata': {}}]
    slack.conversations_members.side_effect = [
        {'members': ['U2'], 'response_metadata': {'next_cursor': 'page2'}},
        {'members': ['U1'], 'response_metadata': {}}]
    assert AnomalyAccess(ledger, slack).authorize('U1', 'CAUDIT') == 'aggregate'
    assert slack.conversations_list.call_count == 2
    assert slack.conversations_members.call_args.kwargs['cursor'] == 'page2'


@pytest.mark.parametrize('response', [
    {'members': None}, {'ok': False, 'members': ['U1']},
    {'members': ['U2'], 'response_metadata': {'next_cursor': 'cycle'}}])
def test_malformed_or_incomplete_channel_membership_fails_closed(anomaly_env, response):
    ledger, _, _, _, _, slack = anomaly_env
    slack.conversations_members.return_value = response
    with pytest.raises(Denied):
        AnomalyAccess(ledger, slack).authorize('U1', 'CAUDIT')


@pytest.mark.parametrize('changes', [{'is_member': False}, {'is_archived': True}, {'is_ext_shared': True}, {'id': 'COTHER'}])
def test_unavailable_or_shared_anomaly_channel_fails_closed(anomaly_env, changes):
    ledger, _, _, _, _, slack = anomaly_env
    slack.conversations_info.side_effect = None
    slack.conversations_info.return_value = {'channel': {'id': 'CAUDIT', 'is_member': True, 'is_archived': False, **changes}}
    with pytest.raises(Denied):
        AnomalyAccess(ledger, slack).authorize('U1', 'CAUDIT')


def test_queue_is_durable_idempotent_and_never_enrolls(anomaly_env):
    ledger, store, _, _, _, slack = anomaly_env
    first = queue_anomaly_request(ledger, slack, 'UANY', 'CAUDIT', 'ask churn trends', 'request1', '123.45')
    second = queue_anomaly_request(ledger, slack, 'UANY', 'CAUDIT', 'ask churn trends', 'request1', '123.45')
    assert first == second
    jobs = store.select('ledger_outbox', {'kind': 'anomaly_query'})
    assert len(jobs) == 1 and jobs[0]['status'] == 'pending'
    assert jobs[0]['payload'] == {'user_id': 'UANY', 'channel': 'CAUDIT', 'text': 'ask churn trends', 'thread_ts': '123.45'}
    assert not store.select('ledger_context', {'kind': 'message'})
    assert not store.select('ledger_participants')
    slack.chat_postMessage.assert_not_called()


@pytest.mark.parametrize('kind', ['message', 'app_mention'])
def test_analytics_routes_before_game_ineligibility_and_broad_counts(anomaly_env, monkeypatch, kind):
    ledger, store, source, composer, api, slack = anomaly_env
    monkeypatch.setenv('SLACKIDS_INELIGIBLE', '["U1"]')
    aggregate = Mock(side_effect=AssertionError('Must not use public count handler'))
    monkeypatch.setattr(source, '_aggregate', aggregate, raising=False)
    event = {'type': kind, 'user': 'U1', 'channel': 'CAUDIT', 'ts': '123.45',
             'text': 'How many members cancelled their subscription this month?'}
    assert Worker(ledger, composer, slack).event(event, 'evt') == 'anomaly_query_queued'
    assert len(store.select('ledger_outbox', {'kind': 'anomaly_query'})) == 1
    aggregate.assert_not_called()
    api.complete.assert_not_called()


@pytest.mark.parametrize('flag', [None, 'false', 'FALSE', ''])
@pytest.mark.parametrize('kind', ['message', 'app_mention'])
def test_disabled_analytics_preserves_normal_message_handling(anomaly_env, monkeypatch, flag, kind):
    ledger, store, _, composer, api, slack = anomaly_env
    if flag is None:
        monkeypatch.delenv('ANOMALY_ENABLED')
    else:
        monkeypatch.setenv('ANOMALY_ENABLED', flag)
    event = {'type': kind, 'user': 'UANY', 'channel': 'CAUDIT', 'ts': '123.45',
             'text': 'What are good predictors of users at risk of dropping out?'}
    assert Worker(ledger, composer, slack).event(event, 'evt') == 'ignored_unlinked_identity'
    assert not route_anomaly_event(ledger, slack, event)
    assert not store.select('ledger_outbox', {'kind': 'anomaly_query'})
    assert not slack.method_calls
    api.complete.assert_not_called()


def test_plain_english_dropping_out_question_routes_to_analytics(anomaly_env):
    ledger, store, _, _, _, slack = anomaly_env
    assert route_anomaly_event(ledger,slack,{'type':'message','user':'U1','channel':'CAUDIT','ts':'12.3',
        'text':'What are good predictors of users at risk of dropping out?'})
    assert len(store.select('ledger_outbox',{'kind':'anomaly_query'})) == 1


@pytest.mark.parametrize('text', ['How many new members joined today?', 'How busy is the space today?',
                                 'My checkout was revoked', 'What happens if my tool checkout is revoked?',
                                 'I noticed an anomaly yesterday'])
def test_unrelated_public_counts_and_ambient_messages_are_not_claimed(anomaly_env, text):
    ledger, _, _, _, _, slack = anomaly_env
    assert not route_anomaly_event(ledger, slack, {'type': 'message', 'user': 'U1', 'channel': 'CAUDIT', 'ts': '1', 'text': text})


@pytest.mark.parametrize('extra', [{'bot_id': 'B1'}, {'app_id': 'A1'}, {'subtype': 'message_changed'}])
def test_bot_and_edited_events_cannot_enqueue_queries(anomaly_env, extra):
    ledger, store, _, _, _, slack = anomaly_env
    assert not route_anomaly_event(ledger, slack, {'type': 'message', 'user': 'U1', 'channel': 'CAUDIT', 'ts': '1', 'text': 'Show anomalies', **extra})
    assert not store.select('ledger_outbox', {'kind': 'anomaly_query'})


def test_slash_request_outside_anomaly_channel_routes_to_own_dm(anomaly_env, monkeypatch):
    ledger, store, _, composer, _, slack = anomaly_env
    monkeypatch.setenv('SLACKIDS_INELIGIBLE', '["U10"]')
    SlackUI(ledger, composer).command({'command': '/ledger-anomaly', 'user_id': 'U10',
        'channel_id': 'COTHER', 'text': 'detectors list', 'trigger_id': 'T1'}, slack)
    job = store.select('ledger_outbox', {'kind': 'anomaly_query'})[0]
    assert job['payload']['channel'] == 'DU10'
    assert job['payload']['text'] == 'detectors list'


@pytest.mark.parametrize('flag', [None, 'false', 'FALSE', ''])
def test_disabled_slash_request_is_rejected_before_slack_calls_or_queueing(anomaly_env, monkeypatch, flag):
    ledger, store, _, composer, api, slack = anomaly_env
    if flag is None:
        monkeypatch.delenv('ANOMALY_ENABLED')
    else:
        monkeypatch.setenv('ANOMALY_ENABLED', flag)
    with pytest.raises(Denied, match='Anomaly analytics is disabled'):
        SlackUI(ledger, composer).command({'command': '/ledger-anomaly', 'user_id': 'U10',
            'channel_id': 'COTHER', 'text': 'detectors list', 'trigger_id': 'T1'}, slack)
    assert not store.select('ledger_outbox', {'kind': 'anomaly_query'})
    assert not store.select('ledger_inbox', {'kind': 'command'})
    assert not slack.method_calls
    api.complete.assert_not_called()


def test_generic_workers_never_claim_anomaly_jobs(anomaly_env):
    ledger, store, _, composer, _, slack = anomaly_env
    key = queue_anomaly_request(ledger, slack, 'U1', 'CAUDIT', 'status', 'request1')
    assert 'anomaly_query' in outbox_filters('outbox')['exclude']
    assert not Worker(ledger, composer, slack).step('ledger_outbox')
    assert store.get('ledger_outbox', key)['status'] == 'pending'
