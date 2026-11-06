import json
from unittest.mock import patch

import pytest
from pymongo import timeout
from pymongo.errors import NetworkTimeout
from slack_sdk import WebClient

from conftest import oid
from ledger import views
from ledger.admin_access import command_eligible, help_text, invite, invitation_eligible
from ledger.authority import Authority
from ledger.conversations import restricted_answer
from ledger.domain import Denied
from ledger.http import HTTPApp
from ledger.slack_app import SlackUI, build_app
from ledger.storage import enqueue
from ledger.worker import Worker
from test_slack import form, request
from test_worker import claim


def member(n):
    return str(oid(n))


@pytest.fixture
def admins(joined, monkeypatch):
    monkeypatch.setenv('LEDGER_QUEST_REVIEW_CHANNEL_ID', 'CREVIEW')
    l, _, _, _, _, slack = joined
    slack.conversations_info.return_value = {'channel': {'is_private': True, 'is_member': True}}
    l.join(member(10))
    return joined


@pytest.mark.parametrize('rank', [0, 1, 2, 6])
def test_admin_command_and_help_use_source_role_at_any_rank(admins, rank):
    l, s, _, comp, _, slack = admins
    p = l.participant(member(10)); p['rank'] = rank; s.put('ledger_participants', p)
    assert command_eligible(l, member(10))
    assert '/ledger-admin invite' in json.dumps(views.home(l, member(10)))
    ui = SlackUI(l, comp)
    ui.command({'user_id': 'U10', 'command': '/ledger-admin', 'text': 'invite', 'trigger_id': 'T'}, slack)
    assert slack.views_open.call_args.kwargs['view']['callback_id'] == 'admin_invitation'


@pytest.mark.parametrize('target', [1, 3, 10])
def test_ineligible_accounts_receive_no_command_or_help(joined, target):
    l, _, _, comp, _, slack = joined
    assert not command_eligible(l, member(target))
    assert help_text(l, member(target)) == ''
    assert '/ledger-admin' not in json.dumps(views.home(l, member(target)))
    assert restricted_answer(l, member(target), 'Try /ledger-admin invite.')
    for text in ('', 'help', 'review', 'invite'):
        with pytest.raises(Denied, match='unavailable'):
            SlackUI(l, comp).command({'user_id': f'U{target}', 'command': '/ledger-admin', 'text': text, 'trigger_id': 'T'}, slack)
    slack.views_open.assert_not_called()


def test_delegates_only_see_scoped_review_help(joined):
    l, _, _, comp, _, slack = joined
    Authority(l).grant(member(10), member(2), ['learning_review'], {'kind': 'global'}, 'Independent reviewer')
    help = help_text(l, member(2))
    assert 'approve' in help and 'ranks' not in help and 'invite' not in help and 'publish-quest' not in help and 'complete-quest' not in help
    SlackUI(l, comp).command({'user_id': 'U2', 'command': '/ledger-admin', 'text': 'help', 'trigger_id': 'T'}, slack)
    assert 'approve' in json.dumps(slack.views_open.call_args.kwargs['view'])
    SlackUI(l, comp).command({'user_id': 'U2', 'command': '/ledger-admin', 'text': '', 'trigger_id': 'T'}, slack)
    assert 'approve' in json.dumps(slack.views_open.call_args.kwargs['view'])
    l.leave(member(2))
    assert help_text(l, member(2)) == ''


def test_invitation_picker_filters_opt_in_revocation_and_invalid_identities(admins):
    l, s, src, comp, _, _ = admins
    src.data['members'][3]['status'] = 'revoked'
    src.data['members'][4]['merged_at'] = 'merged'
    src.data['slack_users'][5]['invalidated_at'] = 'invalid'
    s.put('ledger_catalog', {'_id': 'identity:' + member(7), 'deactivated': True})
    s.put('ledger_catalog', {'_id': 'identity:' + member(8), 'bot': True})
    options = SlackUI(l, comp).options({'user': {'id': 'U10'}, 'action_id': 'invite_recipient', 'value': ''})['options']
    assert {o['value'] for o in options} == {member(3), member(9), member(11)}
    for n in (1, 4, 5, 6, 7, 8, 10):
        with pytest.raises(Denied):
            invite(l, member(10), member(n), '', '', f'invalid-{n}')


@pytest.mark.parametrize('search', ['  aDa   LoVeLaCe ', 'Lovelace Ada', 'ada'])
def test_invitation_picker_matches_name_tokens_case_and_whitespace(admins, search):
    l, _, src, comp, *_ = admins
    src.data['members'][2].update(firstname='Ada', lastname='Lovelace')
    options = SlackUI(l, comp).options({'user': {'id': 'U10'}, 'action_id': 'invite_recipient', 'value': search})['options']
    assert [(o['value'], o['text']['text']) for o in options] == [(member(3), 'Ada Lovelace')]


def test_invitation_picker_preserves_slack_id_fallback_for_nameless_member(admins):
    l, _, src, comp, *_ = admins
    src.data['members'][2].update(firstname='', lastname='')
    options = SlackUI(l, comp).options({'user': {'id': 'U10'}, 'action_id': 'invite_recipient', 'value': 'u3'})['options']
    assert [(o['value'], o['text']['text']) for o in options] == [(member(3), 'U3')]


def test_invitation_picker_searches_before_eligibility_with_constant_database_reads(admins):
    l, s, src, comp, *_ = admins
    for n in range(1000, 2000):
        src.data['members'].append({'_id': oid(n), 'firstname': 'Unrelated', 'lastname': 'Person', 'status': 'activeMember'})
        src.data['slack_users'].append({'_id': oid(n + 10000), 'member_id': oid(n), 'slack_id': f'U{n}'})
    with patch.object(src, 'bounded', wraps=src.bounded) as bounded, patch.object(src, 'member', wraps=src.member) as per_member:
        options = SlackUI(l, comp).options({'user': {'id': 'U10'}, 'action_id': 'invite_recipient', 'value': 'maker3'})['options']
    assert [o['value'] for o in options] == [member(3)]
    assert per_member.call_count < 15  # Actor authorization only; independent of directory size.
    member_queries = [call for call in bounded.call_args_list if call.args[0] == 'members']
    directory_queries = [call for call in member_queries if '$and' in call.args[1]]
    assert len(directory_queries) == 1 and directory_queries[0].args[3] == 500
    # Actor checks now use bounded singleton/member-ID reads too. They stay
    # constant while the one directory query applies search before its cap.
    assert len(member_queries) <= 4
    assert len([call for call in bounded.call_args_list if call.args[0] != 'members']) == 2


@pytest.mark.parametrize('change', ['duplicate_member', 'duplicate_user', 'invalid_uid', 'invalidated', 'revoked', 'merged', 'joined'])
def test_invitation_picker_batch_matches_submission_eligibility(admins, change):
    l, _, src, comp, *_ = admins
    if change == 'duplicate_member':
        src.data['slack_users'].append({'_id': oid(900), 'member_id': oid(3), 'slack_id': 'UOTHER'})
    if change == 'duplicate_user':
        src.data['slack_users'].append({'_id': oid(900), 'member_id': oid(4), 'slack_id': 'U3'})
    if change == 'invalid_uid': src.data['slack_users'][2]['slack_id'] = 'bad uid'
    if change == 'invalidated': src.data['slack_users'][2]['invalidated_at'] = 'invalid'
    if change == 'revoked': src.data['members'][2]['status'] = 'revoked'
    if change == 'merged': src.data['members'][2]['merged_at'] = 'merged'
    if change == 'joined': l.join(member(3))
    assert not invitation_eligible(l, member(3))
    options = SlackUI(l, comp).options({'user': {'id': 'U10'}, 'action_id': 'invite_recipient', 'value': 'Maker3'})['options']
    assert options == []


def test_bare_peer_invite_returns_usage_without_queuing_work(joined):
    l, s, _, comp, _, slack = joined
    before = s.select('ledger_inbox')
    with pytest.raises(ValueError, match=r'Use /ledger invite @member'):
        SlackUI(l, comp).command({'user_id': 'U1', 'command': '/ledger', 'text': 'invite', 'trigger_id': 'T'}, slack)
    assert s.select('ledger_inbox') == before
    SlackUI(l, comp).command({'user_id': 'U1', 'command': '/ledger', 'text': 'invite <@U2> chat', 'trigger_id': 'T'}, slack)
    assert any(j.get('payload', {}).get('command') == '/ledger invite <@U2> chat' for j in s.select('ledger_inbox'))


def test_signed_picker_callback_finds_member_with_database_deadline(admins):
    l, store, _, composer, *_ = admins
    ui = SlackUI(l, composer)
    app = HTTPApp(build_app(ui, 'xoxb-test', 'test-signing-secret', 'T1', 'UBOT', WebClient(token='xoxb-test')), store)
    payload = {'type': 'block_suggestion', 'team': {'id': 'T1'}, 'user': {'id': 'U10'},
               'action_id': 'invite_recipient', 'value': 'Maker3', 'view': views.admin_invitation()}
    with patch('ledger.slack_app.timeout', wraps=timeout) as deadline:
        status, response = request(app, {'payload': json.dumps(payload)}, 'application/x-www-form-urlencoded', path='/slack/interactions')
    assert status == 200 and [o['value'] for o in json.loads(response)['options']] == [member(3)]
    deadline.assert_called_once_with(2)


def test_signed_picker_database_timeout_returns_empty_options_without_killing_request(admins, caplog):
    l, store, src, composer, *_ = admins
    app = HTTPApp(build_app(SlackUI(l, composer), 'xoxb-test', 'test-signing-secret', 'T1', 'UBOT', WebClient(token='xoxb-test')), store)
    payload = {'type': 'block_suggestion', 'team': {'id': 'T1'}, 'user': {'id': 'U10'},
               'action_id': 'invite_recipient', 'value': 'Maker3', 'view': views.admin_invitation()}
    with patch.object(src, 'bounded', side_effect=NetworkTimeout('PRIVATE_CONNECTION_DETAILS')):
        status, response = request(app, {'payload': json.dumps(payload)}, 'application/x-www-form-urlencoded', path='/slack/interactions')
    assert status == 200 and json.loads(response) == {'options': []}
    assert 'error_type=NetworkTimeout' in caplog.text
    assert 'PRIVATE_CONNECTION_DETAILS' not in response + caplog.text


@pytest.mark.parametrize('sender,expected', [('The Archivist', 'The Archivist'), ('', 'Maker10 Test')])
def test_modal_invitation_keeps_sender_private_and_does_not_enroll(admins, sender, expected):
    l, s, _, comp, api, slack = admins
    ui = SlackUI(l, comp)
    view = views.admin_invitation()
    body = form(view, {'invite_recipient': member(3), 'sender': sender, 'message': 'Come make something!'}, 'U10')
    ui.submission(body, slack)
    ui.submission(body, slack)
    jobs = s.select('ledger_outbox', {'kind': 'admin_invitation'})
    assert len(jobs) == 1
    Worker(l, comp, slack).outbox(claim(s, jobs[0]['_id']))
    sent = slack.chat_postMessage.call_args.kwargs
    assert sent['channel'] == 'DU3' and expected in sent['text'] and 'Come make something!' in sent['text']
    if sender:
        assert 'Maker10' not in json.dumps(sent) and 'U10' not in json.dumps(sent) and member(10) not in json.dumps(sent)
    assert sent['blocks'][-1]['elements'][0]['value'] == ''
    assert not l.participant(member(3))
    api.complete.assert_not_called()


@pytest.mark.parametrize('change', ['actor_leave', 'role_loss', 'recipient_join', 'recipient_revoked'])
def test_invitation_rechecks_delivery_authorization(admins, change):
    l, s, src, comp, _, slack = admins
    invite(l, member(10), member(3), 'The Archivist', '', 'stale')
    if change == 'actor_leave': l.leave(member(10))
    if change == 'role_loss': src.data['members'][9]['role'] = 'member'
    if change == 'recipient_join': l.join(member(3))
    if change == 'recipient_revoked': src.data['members'][2]['status'] = 'revoked'
    with pytest.raises(Denied): Worker(l, comp, slack).outbox(claim(s, 'admin-invitation:stale'))
    slack.chat_postMessage.assert_not_called()


def test_stale_invitation_modal_and_help_are_blocked_on_opt_out(admins):
    l, s, _, comp, _, slack = admins
    ui = SlackUI(l, comp)
    Worker(l, comp, slack).admin_command(member(10), ['help'], 'admin-help')
    l.leave(member(10))
    with pytest.raises(Denied): ui.submission(form(views.admin_invitation(), {'invite_recipient': member(3)}, 'U10'), slack)
    with pytest.raises(Denied): Worker(l, comp, slack).outbox(claim(s, 'dm:admin-help'))
    slack.chat_postMessage.assert_not_called()


def test_admin_review_invite_and_opt_out_removal_run_while_paused(admins):
    l, s, _, comp, api, slack = admins
    w = Worker(l, comp, slack)
    s.put('ledger_catalog', {'_id': 'control', 'paused': True})
    assert w.step('ledger_outbox', kinds=['review_channel_invite'])
    slack.conversations_invite.assert_called_once_with(channel='CREVIEW', users='U10')
    l.leave(member(10))
    assert w.step('ledger_outbox', kinds=['remove'])
    assert any(c.kwargs == {'channel': 'CREVIEW', 'user': 'U10'} for c in slack.conversations_kick.call_args_list)
    api.complete.assert_not_called()


def test_only_source_admins_receive_automatic_review_invites(admins):
    l, s, src, comp, _, slack = admins
    l.join(member(11))
    src.data['members'][2]['role'] = 'resource_manager'
    l.join(member(3))
    jobs = s.select('ledger_outbox', {'kind': 'review_channel_invite'})
    assert [j['payload']['member_id'] for j in jobs] == [member(10)]
    assert Worker(l, comp, slack).step('ledger_outbox', kinds=['review_channel_invite'])
    src.data['members'][9]['role'] = 'member'
    l.reconcile(member(10))
    removal = next(j for j in s.select('ledger_outbox', {'kind': 'remove'}) if j['payload'].get('review_channel'))
    Worker(l, comp, slack).outbox(claim(s, removal['_id']))
    slack.conversations_kick.assert_called_once_with(channel='CREVIEW', user='U10')


def test_delayed_review_invite_does_not_survive_leave_or_consent_generation(admins):
    l, s, _, comp, _, slack = admins
    job = s.select('ledger_outbox', {'kind': 'review_channel_invite'})[0]
    l.leave(member(10)); l.join(member(10))
    with pytest.raises(Denied): Worker(l, comp, slack).outbox(claim(s, job['_id']))
    assert Worker(l, comp, slack).step('ledger_outbox', kinds=['review_channel_invite'])
    removal = next(j for j in s.select('ledger_outbox', {'kind': 'remove'}) if j['payload'].get('review_channel'))
    Worker(l, comp, slack).outbox(claim(s, removal['_id']))
    slack.conversations_kick.assert_not_called()


def test_opt_out_during_review_invite_is_compensated(admins):
    l, _, _, comp, _, slack = admins
    slack.conversations_invite.side_effect = lambda **kwargs: l.leave(member(10))
    assert Worker(l, comp, slack).step('ledger_outbox', kinds=['review_channel_invite'])
    slack.conversations_kick.assert_called_once_with(channel='CREVIEW', user='U10')


@pytest.mark.parametrize('info', [{'is_private': False, 'is_member': True}, {'is_private': True, 'is_member': False}, {'is_private': True, 'is_member': True, 'is_ext_shared': True}])
def test_review_invites_validate_private_channel(admins, info):
    l, s, _, comp, _, slack = admins
    slack.conversations_info.return_value = {'channel': info}
    job = s.select('ledger_outbox', {'kind': 'review_channel_invite'})[0]
    with pytest.raises(ValueError): Worker(l, comp, slack).outbox(claim(s, job['_id']))
    slack.conversations_invite.assert_not_called()


def test_admin_roles_from_chat_and_rank_do_not_grant_command(joined):
    l, s, _, comp, _, slack = joined
    p = l.participant(member(1)); p.update(rank=6, role='admin'); s.put('ledger_participants', p)
    assert not command_eligible(l, member(1))
    l.notify(member(1), 'status', {'summary': 'General member information'}, 'generated-help')
    comp.api.complete.return_value = 'Use /ledger-admin invite.'
    Worker(l, comp, slack).outbox(claim(s, 'dm:generated-help'))
    assert '/ledger-admin' not in json.dumps(slack.chat_postMessage.call_args.kwargs)


def test_review_membership_reconciles_configuration_and_retries_failed_invitation(admins, monkeypatch):
    l, s, _, comp, _, slack = admins
    w = Worker(l, comp, slack)
    job = s.select('ledger_outbox', {'kind': 'review_channel_invite'})[0]
    job['status'] = 'failed'; s.put('ledger_outbox', job)
    l.reconcile(member(10))
    assert w.step('ledger_outbox', kinds=['review_channel_invite'])
    monkeypatch.setenv('LEDGER_QUEST_REVIEW_CHANNEL_ID', 'CNEW')
    l.reconcile(member(10))
    assert w.step('ledger_outbox', kinds=['remove'])
    slack.conversations_kick.assert_called_once_with(channel='CREVIEW', user='U10')
    assert w.step('ledger_outbox', kinds=['review_channel_invite'])
    assert slack.conversations_invite.call_args.kwargs['channel'] == 'CNEW'
    assert not s.select('ledger_channels', {'kind': 'channel', 'channel_id': 'CNEW'})


def test_empty_configuration_does_not_queue_review_invite(joined, monkeypatch):
    l, s, *_ = joined
    monkeypatch.delenv('LEDGER_QUEST_REVIEW_CHANNEL_ID', raising=False)
    l.join(member(10))
    assert not s.select('ledger_outbox', {'kind': 'review_channel_invite'})


def test_review_cleanup_uses_saved_slack_identity_when_mapping_is_lost(admins):
    l, s, src, comp, _, slack = admins
    w = Worker(l, comp, slack)
    assert w.step('ledger_outbox', kinds=['review_channel_invite'])
    src.data['slack_users'][9]['invalidated_at'] = 'invalid'
    l.reconcile(member(10))
    assert w.step('ledger_outbox', kinds=['remove'])
    slack.conversations_kick.assert_called_once_with(channel='CREVIEW', user='U10')
