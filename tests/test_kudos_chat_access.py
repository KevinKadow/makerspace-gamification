import json
from unittest.mock import patch

import pytest

from conftest import oid
from ledger import views
from ledger.conversations import converse, conversation_facts
from ledger.domain import Denied
from ledger.query_tools import QueryTools
from ledger.slack_app import SlackUI
from ledger.worker import Worker
from test_slack import form
from test_worker import claim


def mid(n):
    return str(oid(n))


@pytest.mark.parametrize('emoji', [':poop:', ':hankey:', ':shit:', ':-1:', ':thumbsdown:', ':middle_finger:',
                                 ':clown_face:', ':middle_finger::skin-tone-3:', '💩', '🖕🏾', ':forged:<@U3>'])
def test_rejected_picker_selection_drops_only_emoji(env, emoji):
    l, s, _, composer, _, slack = env
    original = '*Keep* :poop: and Qwen exactly as authored.'
    receipt = l.kudos(mid(1), mid(2), original, key='emoji', emoji=emoji, expected_participation=False, public=True)
    assert receipt['emoji'] == '' and receipt['message'] == original
    w = Worker(l, composer, slack)
    w.outbox(claim(s, 'kudos:emoji:recipient'))
    dm = slack.chat_postMessage.call_args.kwargs
    assert dm['text'] == 'You have received kudos from <@U1>\n' + original
    assert dm['blocks'][2]['text']['text'] == original
    w.outbox(claim(s, 'kudos:emoji:shared'))
    assert slack.chat_postMessage.call_args.kwargs['text'] == '<@U2> has received kudos from <@U1>\n' + original
    assert not s.select('ledger_participants')


@pytest.mark.parametrize('selected', [':clap:', ':kudo:', ':kudos:', ':fix_parrot:', ':first_place_medal:',
                                    ':fistbump:', ':duct_tape:', ':thankyou:', ':teamwork:'])
def test_nonparticipant_kudos_full_form_options_updates_and_receipts(env, selected):
    l, s, _, composer, _, slack = env
    ui = SlackUI(l, composer)
    ui.command({'user_id': 'U1', 'command': '/kudos', 'text': '', 'trigger_id': 'T'}, slack)
    first = slack.views_open.call_args.kwargs['view']
    assert ui.options({'user': {'id': 'U1'}, 'action_id': 'recipient', 'value': '', 'view': first})['options']
    detail = ui.submission(form(first, {'recipient': mid(2)}), slack)['view']
    emoji = next(b for b in detail['blocks'] if b.get('block_id') == 'emoji')
    assert emoji['optional'] and emoji['element']['type'] == 'static_select'
    assert [o['value'] for o in emoji['element']['options'][:8]] == [
        ':kudo:', ':kudos:', ':fix_parrot:', ':first_place_medal:', ':fistbump:', ':duct_tape:', ':thankyou:', ':teamwork:']
    fields = {'emoji': selected, 'message': '*Thanks!*', 'public': True, 'invitation': 'yes', 'shop': mid(201)}
    body = form(detail, fields)
    body['actions'] = [{'action_id': 'shop'}]
    ui.action(body, slack)
    updated = slack.views_update.call_args.kwargs['view']
    assert slack.views_update.call_args.kwargs['hash'] == 'h1'
    assert next(b for b in updated['blocks'] if b.get('block_id') == 'emoji')['element']['initial_option']['value'] == selected
    fields['tool'] = mid(311)
    ui.submission(form(updated, fields), slack)
    receipt = s.select('ledger_evidence', {'kind': 'kudos'})[0]
    assert receipt['emoji'] == selected and not receipt['xp_awarded']
    assert not s.select('ledger_relationships', {'kind': 'sponsor'})
    w = Worker(l, composer, slack)
    for audience in ('recipient', 'shared'):
        w.outbox(claim(s, receipt['_id'] + ':' + audience))
        assert selected in slack.chat_postMessage.call_args.kwargs['text']
    assert s.get('ledger_outbox', 'ack:' + receipt['_id']) is None
    owner = s.get('ledger_evidence', receipt['summary_id'])
    flush = next(j for j in s.select('ledger_outbox', {'kind': 'summary_flush'})
                 if j['payload']['summary_id'] == owner['_id'])
    w.outbox(claim(s, flush['_id']))
    delivery = s.get('ledger_outbox', s.get('ledger_evidence', owner['_id'])['snapshot_job_id'])
    assert delivery['payload']['exception']
    w.outbox(claim(s, delivery['_id']))
    assert 'Your kudos reached <@U2> in DM and Ledge Chat. No XP was awarded.' in slack.chat_postMessage.call_args.kwargs['text']
    assert not s.select('ledger_participants')


def test_nonparticipant_giver_preserves_recipient_repeat_caps(joined):
    l, s, *_ = joined
    for i in range(7):
        l.kudos(mid(3), mid(2), 'Thank you', key=str(i), expected_participation=True)
    assert l.participant(mid(2))['xp'] == '17'
    assert l.participant(mid(3)) is None
    assert len(s.select('ledger_outbox', {'kind': 'kudos'})) == 7
    l.kudos(mid(3), mid(2), 'Changed retry', key='0', emoji=':clap:', expected_participation=True)
    assert s.get('ledger_evidence', 'kudos:0')['emoji'] == ''


@pytest.mark.parametrize('channel,text,thread', [
    ('DU1', 'Tell me about XP', None), ('CSHOP', 'The Ledger, tell me about XP', None),
    ('CSHOP', '<@UBOT> tell me about XP', None), ('CSHOP', 'More please', '10.1'),
])
def test_nonparticipant_chat_routes_with_join_action(env, channel, text, thread):
    l, s, _, composer, api, slack = env
    w = Worker(l, composer, slack, bot_id='UBOT')
    api.tool_response.return_value = {'content': 'Use /ledger join to see behind the curtain.'}
    if thread:
        w.event({'type': 'message', 'user': 'UBOT', 'channel': channel, 'ts': thread, 'text': 'A bot post'}, 'bot-post')
    event = {'type': 'message', 'user': 'U1', 'channel': channel, 'ts': '11.1', 'text': text}
    if thread:
        event['thread_ts'] = thread
    assert w.event(event, 'ev') == 'reply_queued'
    assert not api.complete.called
    w.outbox(claim(s, 'reply:' + channel + ':11.1'))
    call = slack.chat_postMessage.call_args.kwargs
    assert call['channel'] == channel and call['thread_ts'] == (thread or '11.1')
    assert call['blocks'][1]['elements'][0]['action_id'] == 'join'
    prompt = json.dumps((api.tool_response.call_args or api.complete.call_args).args[0])
    assert 'Newbie' not in prompt and 'Novice' not in prompt and 'Adept' not in prompt
    assert 'seed rank matrix' not in prompt.lower()
    assert '/ledger join' in prompt
    assert not l.participant(mid(1))


def test_question_anywhere_can_be_ignored_without_marking_bot_thread(env):
    l, s, _, composer, api, slack = env
    w = Worker(l, composer, slack, bot_id='UBOT')
    api.tool_response.return_value = {'content': 'NO_REPLY'}
    assert w.event({'type': 'message', 'user': 'U1', 'channel': 'CCHAT', 'ts': '12.1',
                    'text': 'Lunch? I packed a sandwich.'}, 'ambient') == 'reply_queued'
    w.outbox(claim(s, 'reply:CCHAT:12.1'))
    slack.chat_postMessage.assert_not_called()
    assert not s.get('ledger_context', 'thread:CCHAT:12.1')
    assert not s.select('ledger_evidence', {'kind': 'ai_observation'})
    assert w.event({'type': 'message', 'user': 'U1', 'channel': 'CCHAT', 'ts': '13.1', 'text': 'More',
                    'thread_ts': '12.1'}, 'more') == 'ignored_unaddressed_channel_message'


@pytest.mark.parametrize('fixture', ['env', 'joined'])
@pytest.mark.parametrize('text', ['Lunch? I packed a sandwich.', 'How is my progress?', 'Show my stats'])
def test_unrelated_ambient_messages_never_reach_context_or_inference(request, fixture, text):
    l, s, _, composer, api, slack = request.getfixturevalue(fixture)
    w = Worker(l, composer, slack, bot_id='UBOT')
    assert w.event({'type': 'message', 'user': 'U1', 'channel': 'COTHER', 'ts': '12.1',
                    'text': text, 'thread_ts': '10.1'}, 'ambient') == 'ignored_unaddressed_channel_message'
    assert not s.get('ledger_context', 'message:COTHER:12.1')
    assert not s.get('ledger_outbox', 'reply:COTHER:12.1')
    api.complete.assert_not_called()
    api.tool_response.assert_not_called()


@pytest.mark.parametrize('text', ['Lunch? I packed a sandwich.', 'Show my stats'])
def test_queued_ambient_and_progress_jobs_are_blocked_if_channel_is_unregistered(joined, text):
    l, s, _, composer, api, slack = joined
    w = Worker(l, composer, slack, bot_id='UBOT')
    assert w.event({'type': 'message', 'user': 'U1', 'channel': 'CCHAT', 'ts': '12.1', 'text': text}, 'ambient') == 'reply_queued'
    legacy_request = s.get('ledger_context', 'message:CCHAT:12.1')
    legacy_request.pop('conversation_requested')
    s.put('ledger_context', legacy_request)
    s.delete('ledger_channels', 'chat')
    with pytest.raises(Denied, match='registered Ledger channel'):
        w.outbox(claim(s, 'reply:CCHAT:12.1'))
    api.complete.assert_not_called()
    api.tool_response.assert_not_called()
    assert not s.get('ledger_outbox', 'reply:CCHAT:12.1').get('composed')


def test_unrelated_explicit_request_excludes_legacy_unrequested_context(env):
    l, s, _, composer, api, slack = env
    w = Worker(l, composer, slack, bot_id='UBOT')
    for ts, extra in [('9.1', {}), ('9.2', {'conversation_requested': False})]:
        s.put('ledger_context', {'_id': f'message:COTHER:{ts}', 'kind': 'message', 'member_id': mid(2),
                                'channel': 'COTHER', 'thread': '10.1', 'text': 'UNRELATED_PRIVATE_CONVERSATION', 'at': ts, **extra})
    assert w.event({'type': 'message', 'user': 'U1', 'channel': 'COTHER', 'ts': '12.1', 'thread_ts': '10.1',
                    'text': '<@UBOT> Tell me about XP'}, 'mention') == 'reply_queued'
    api.tool_response.return_value = {'content': 'The Ledger recognizes learning with XP.'}
    w.outbox(claim(s, 'reply:COTHER:12.1'))
    assert 'UNRELATED_PRIVATE_CONVERSATION' not in json.dumps(api.tool_response.call_args.args[0])
    slack.chat_postMessage.assert_called_once()


@pytest.mark.parametrize('channel,text,thread', [('DU1', 'Show my stats', None),
                                               ('COTHER', '<@UBOT> Show my stats', None),
                                               ('COTHER', 'Show my stats', '10.1')])
def test_direct_progress_requests_retain_tool_routing(joined, channel, text, thread):
    l, s, _, composer, api, slack = joined
    w = Worker(l, composer, slack, bot_id='UBOT')
    if thread:
        w.event({'type': 'message', 'user': 'UBOT', 'channel': channel, 'ts': thread, 'text': 'A bot post'}, 'bot')
    event = {'type': 'message', 'user': 'U1', 'channel': channel, 'ts': '12.1', 'text': text}
    if thread:
        event['thread_ts'] = thread
    assert w.event(event, 'direct') == 'reply_queued'
    job = claim(s, f'reply:{channel}:12.1')
    assert job['payload']['progress_request'] and job['payload']['use_tools']
    api.tool_response.return_value = {'content': 'The Ledger has your calculated progress.'}
    w.outbox(job)
    api.tool_response.assert_called_once()


def test_bot_published_kudos_root_allows_followup_without_exposing_body(env):
    l, s, _, composer, api, slack = env
    l.kudos(mid(1), mid(2), 'PRIVATE_KUDOS_BODY', key='root', public=True, expected_participation=False)
    w = Worker(l, composer, slack, bot_id='UBOT')
    w.outbox(claim(s, 'kudos:root:shared'))
    api.tool_response.return_value = {'content': 'The Ledger records learning and contribution.'}
    assert w.event({'type': 'message', 'user': 'U2', 'channel': 'CCHAT', 'ts': '200.1',
                    'thread_ts': '123.456', 'text': 'Thanks, tell me about The System'}, 'followup') == 'reply_queued'
    w.outbox(claim(s, 'reply:CCHAT:200.1'))
    assert 'PRIVATE_KUDOS_BODY' not in json.dumps(api.tool_response.call_args.args[0])


def test_chat_live_identity_joined_channel_and_consent_changes(env):
    l, s, _, composer, api, slack = env
    w = Worker(l, composer, slack, bot_id='UBOT')
    slack.conversations_info.return_value = {'channel': {'is_member': False}}
    assert w.event({'type': 'app_mention', 'user': 'U1', 'channel': 'COTHER', 'ts': '1', 'text': 'Hi'}, 'e') == 'ignored_unjoined_channel'
    slack.conversations_info.return_value = {'channel': {'is_member': True}}
    w.event({'type': 'message', 'user': 'U1', 'channel': 'COTHER', 'ts': '2', 'text': 'The Ledger, hello'}, 'e2')
    slack.conversations_info.return_value = {'channel': {'is_member': False}}
    with pytest.raises(Denied, match='no longer in this channel'):
        w.outbox(claim(s, 'reply:COTHER:2'))
    w.event({'type': 'message', 'user': 'U1', 'channel': 'DU1', 'ts': '3', 'text': 'Hello'}, 'e3')
    api.complete.side_effect = lambda *a: (l.join(mid(1)), 'Hello')[1]
    with pytest.raises(Denied):
        w.outbox(claim(s, 'reply:DU1:3'))
    slack.chat_postMessage.assert_not_called()


def test_rank_and_quest_facts_are_bounded_to_caller(joined):
    l, s, _, composer, api, *_ = joined
    p = l.participant(mid(1))
    p.update(rank=2, import_pending=False)
    s.put('ledger_participants', p)
    s.put('ledger_quests', {'_id': 'secret', 'kind': 'member_quest', 'target_rank': 5, 'title': 'SECRET_HIGH_QUEST'})
    facts = conversation_facts(l, mid(1), True, 'Explain my ranks, rules and quests')
    assert [r['slot'] for r in facts['ranks']] == [1, 2]
    assert 'next_rank' not in facts['progress'] and facts['progress']['milestones']
    assert 'Initiate' not in json.dumps(facts) and 'SECRET_HIGH_QUEST' not in json.dumps(facts)
    api.complete.return_value = 'Your next rank is Initiate.'
    answer = converse(l, composer, mid(1), 'What is next?', use_tools=False)
    assert answer['outcome'] == 'fallback' and 'Initiate' not in answer['text']
    api.complete.return_value = 'You are Novice; Newbie is the lower rank.'
    assert converse(l, composer, mid(1), 'What rank am I?', use_tools=False)['outcome'] == 'generated'
    l.leave(mid(1))
    api.complete.return_value = 'Use /ledger join to take the red pill and start your journey.'
    assert converse(l, composer, mid(1), 'What is XP?', use_tools=False)['outcome'] == 'generated'
    assert 'xp' not in conversation_facts(l, mid(1), True, 'What is XP?')


def test_nonparticipant_shop_tool_queries_use_public_projection_and_unknown(env):
    l, _, src, composer, api, *_ = env
    src.data['tools'][0].update(description='Bench drill press', wiki_url='https://wiki.example/drill', notes='PRIVATE_TOOL_NOTES')
    src.data['shops'][0].update(wiki_url='https://wiki.example/shop', out_of_service=True, out_of_service_note='Maintenance',
                                outage_actor_name='PRIVATE_ACTOR')
    tool = QueryTools(l, mid(1)).call('query_makerspace', {'collection': 'tools', 'search': 'Tool1-1'})['results'][0]
    assert tool['description'] == 'Bench drill press' and tool['wiki_url']
    assert 'notes' not in tool
    shop = QueryTools(l, mid(1)).call('query_makerspace', {'collection': 'shops', 'search': 'Shop1'})['results'][0]
    assert shop['out_of_service_note'] == 'Maintenance' and 'outage_actor_name' not in shop
    for name, args in [('my_progress', {}), ('query_makerspace', {'collection': 'tool_checkouts'}),
                       ('query_makerspace', {'collection': 'volunteer_tasks'})]:
        with pytest.raises(Denied):
            QueryTools(l, mid(1)).call(name, args)
    api.tool_response.side_effect = [
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'read', 'type': 'function', 'function': {
            'name': 'query_makerspace', 'arguments': json.dumps({'collection': 'tools', 'search': 'Tool1-1'})}}]},
        {'role': 'assistant', 'content': 'Tool1-1 is described as a bench drill press.'}]
    result = converse(l, composer, mid(1), 'What is Tool1-1?', history=[{'role': 'user', 'content': 'I am asking about the drill press.'}])
    assert result['outcome'] == 'generated'
    sent = api.tool_response.call_args.args[0]
    assert sent[-1]['role'] == 'tool' and 'Bench drill press' in sent[-1]['content']
    assert 'PRIVATE_TOOL_NOTES' not in json.dumps(sent)
    assert len(api.tool_response.call_args.args[1]) == 1
    api.tool_response.side_effect = TimeoutError()
    assert converse(l, composer, mid(1), 'Does Tool1-1 cut titanium?')['text'] == "I don't know."


def test_shop_tool_answers_without_retrieval_or_empty_results_do_not_guess(env):
    l, _, _, composer, api, *_ = env
    api.tool_response.return_value = {'content': 'Every tool can cut titanium.'}
    assert converse(l, composer, mid(1), 'Can your tools cut titanium?')['text'] == "I don't know."
    api.tool_response.side_effect = [
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'read', 'type': 'function', 'function': {
            'name': 'query_makerspace', 'arguments': json.dumps({'collection': 'tools', 'search': 'UNKNOWN'})}}]},
        {'content': 'The unknown tool certainly cuts titanium.'}]
    assert converse(l, composer, mid(1), 'Can this tool cut titanium?')['text'] == "I don't know."


def test_dm_history_keeps_author_and_bot_reply_but_drops_previous_consent_epoch(joined):
    l, s, _, composer, api, slack = joined
    w = Worker(l, composer, slack, bot_id='UBOT')
    api.complete.return_value = 'Recorded conversation context.'
    w.event({'type': 'message', 'user': 'U1', 'channel': 'DU1', 'ts': '10', 'text': 'Earlier question'}, 'before')
    w.outbox(claim(s, 'reply:DU1:10'))
    w.event({'type': 'message', 'user': 'U1', 'channel': 'DU1', 'ts': '200', 'text': 'More please'}, 'after')
    w.outbox(claim(s, 'reply:DU1:200'))
    prompt = api.complete.call_args.args[0]
    assert any(m['role'] == 'assistant' and m['content'] == 'Recorded conversation context.' for m in prompt)
    assert any('Author U1: Earlier question' in m['content'] for m in prompt)
    l.leave(mid(1))
    w.event({'type': 'message', 'user': 'U1', 'channel': 'DU1', 'ts': '300', 'text': 'Explain XP'}, 'out')
    w.outbox(claim(s, 'reply:DU1:300'))
    assert 'Earlier question' not in json.dumps(api.complete.call_args.args[0])
