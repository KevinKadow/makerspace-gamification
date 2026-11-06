import ast
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree

import pytest

from conftest import oid
from ledger.domain import Denied
from ledger.messages import Composer, PERSONA
from ledger.prompt_library import EXAMPLE_FACTS
from ledger.prompt_matrix import (MAX_MATRIX_BYTES, REQUIRED_ROLES, PromptMatrix, bundled_matrix,
                                  fetch_google_doc, google_doc_id, validate_matrix)
from ledger.rules import RANKS, XP
from ledger.worker import Worker
from test_prompt_rotation import message
from test_worker import claim

DOC = 'https://docs.google.com/document/d/example_document_id/edit?usp=sharing#heading=h.test'


def policy(version='1'):
    matrix = bundled_matrix()
    return matrix['text'].replace(f'id="the-ledger" version="{matrix["version"]}"', f'id="the-ledger" version="{version}"')


def fake_connection(raw, *, status=200, content_type='text/plain; charset=utf-8', location=None):
    response = Mock(status=status)
    headers = {'Content-Type': content_type, 'Location': location}
    response.getheader.side_effect = lambda key, default=None: headers.get(key, default)
    response.read1.side_effect = BytesIO(raw if isinstance(raw, bytes) else raw.encode()).read1
    connection = Mock()
    connection.getresponse.return_value = response
    return connection


def test_bundled_matrix_codifies_roles_and_seed_economy():
    matrix = bundled_matrix()
    root = ElementTree.fromstring(matrix['text'])
    admin = root.find("roles/role[@id='admin']").text
    assert 'MLAB members role admin' in admin and 'every rank' in admin
    assert 'sender display name replaces the admin real name' in admin
    assert 'on opt-out remove them' in admin
    assert 'Omit /ledger-admin & related help for ineligible callers' in root.find('response').text
    generated_author = root.find("roles/role[@id='ledger_quest_author']").text
    privacy = root.find('privacy').text
    assert 'Never announce rank-ups in shared Ledge Chat' in root.find('channels').text
    assert 'quest/discipline prose' in generated_author and 'human publication edits' in generated_author
    assert 'anywhere in retained chat or completed-example prose at every rank' in privacy
    assert 'cooperative discipline prose' in privacy and 'fitted saved snapshot at composition/submission' in privacy
    assert 'Ledge Chat and the selected rank channel' in privacy
    assert 'including nonparticipants,non-authors and inactive members' in privacy
    assert 'complete bounded Slack/makerspace directories' in privacy
    assert 'incomplete identity reads omit chat' in privacy
    assert 'names absent from both directories may remain' in privacy
    assert 'Directories never enter saved context/inference' in privacy
    assert 'configured rank names are forbidden' in generated_author
    assert 'never completion finalization time' in root.find('economy').text
    assert 'quest_revision matches the disabled revision' in root.find('community').text
    consent = root.find('consent').text
    assert 'first 150 characters' in consent and 'literal first/last-name tokens in any order' in consent
    assert 'valid Slack ID' in consent and '500 candidates/100 choices' in consent
    assert 'invalid/ambiguous links & known bots/deactivated identities' in consent
    assert 'Incomplete identity reads or database errors return no choices' in consent
    assert 'two-second database deadline' in consent and 'Discovery grants no consent' in consent
    assert 'legacy done jobs without receipts' in consent and 'confirmed same-generation receipts' in consent
    normalized_bytes = len(matrix['text'].encode('utf-8'))
    crlf_bytes = len(matrix['text'].replace('\n', '\r\n').encode('utf-8'))
    assert normalized_bytes <= 30 * 1024
    assert crlf_bytes <= 30 * 1024
    assert crlf_bytes <= MAX_MATRIX_BYTES
    assert {r.get('id') for r in root.find('roles')} == REQUIRED_ROLES
    assert 'Repeated join requests show saved participation' in root.find('consent').text
    assert 'In registered Ledger channels only' in root.find('channels').text
    assert 'reject stale ambient jobs before inference' in root.find('channels').text
    channels = root.find('channels').text
    assert 'post a generic ascent in the prior rank channel without hinting at the next' in channels
    assert 'then remove from the prior & welcome only after invite succeeds' in channels
    assert 'Python supplies stage-limited facts' in channels and 'rejects next-rank names' in channels
    assert 'Bind reserved JPEG claims to the consent generation' in channels
    assert 'saved member identity' in channels and 'terminal failure of the owning Slack event' in channels
    assert 'workspace authentication/scope failures' in channels
    assert 'distinct specialized evidence attempt' in root.find('economy').text
    delegate = root.find("roles/role[@id='delegated_reviewer']").text
    assert 'stable logical ID' in delegate and 'actual-shop scope per operation' in delegate
    assert 'authorized pending contributions/ready shared projects' in delegate
    assert 'private channel membership is unnecessary' in delegate
    assert 'history-import pending' in root.find('response').text
    assert 'cancels invalid observations individually before inference' in root.find("roles/role[@id='ai_observer']").text
    observer = root.find("roles/role[@id='ai_observer']").text
    assert 'regardless of game opt-in' in observer and 'Member opt-out & deployment disable' in observer
    nonparticipant = root.find("roles/role[@id='nonparticipant']").text
    assert 'May send/receive peer kudos' in nonparticipant
    assert '/ledger preferences opts out without joining' in consent
    assert 'No rules,specific ranks/quests or retained progress disclosure' in nonparticipant
    assert 'persists across upgrades/join/rejoin' in root.find('consent').text
    assert 'configured private staff channel' in root.find("roles/role[@id='ledger']").text
    assert 'Save review_message_ts/review_channel_id' in root.find('community').text
    assert 'if deleted,post a replacement & save its timestamp' in root.find('community').text
    assert 'skip current fingerprints & settled history writes' in root.find('community').text
    assert 'immutable version-specific IDs' in root.find('community').text
    for i, (name, _, floor, _) in enumerate(RANKS, 1):
        assert f'| {i} | {name} | {floor if floor is not None else "Inactive"} |' in root.find('progression').text
    for rate in XP.values():
        assert f'| {rate} |' in root.find('economy').text
    # Catch new literal authorization roles in the application, not only hand-maintained IDs.
    found = set()
    for path in (Path(__file__).parents[1] / 'ledger').glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Call) and isinstance(node.left.func, ast.Attribute):
                call = node.left
                if call.func.attr == 'role' or (call.func.attr == 'get' and call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value == 'role'):
                    for comparison in node.comparators:
                        found.update(n.value for n in ast.walk(comparison) if isinstance(n, ast.Constant) and isinstance(n.value, str))
    assert {'admin', 'board_member', 'resource_manager'} <= found <= REQUIRED_ROLES


@pytest.mark.parametrize('role', ['quest_author', 'ledger_quest_author', 'ai_observer', 'delegated_reviewer'])
def test_missing_new_required_remote_role_retains_current_policy(role):
    import re
    matrix = PromptMatrix(DOC)
    before = matrix.snapshot()
    incomplete = re.sub(r'<role id="' + role + r'">.*?</role>', '', before['text'], flags=re.S)
    with patch('ledger.prompt_matrix.fetch_google_doc', return_value=incomplete):
        assert matrix.refresh('missing-role')['outcome'] == 'bundled_fallback'
    assert matrix.snapshot() == before


@pytest.mark.parametrize('transform', [
    lambda text: '<html>Sign in</html>',
    lambda text: text.replace('<role id="admin">', '<role id="unknown">'),
    lambda text: text.replace('<role id="board_member">', '<role id="admin">'),
    lambda text: text.replace('<identity>', '<unexpected>').replace('</identity>', '</unexpected>'),
    lambda text: text.replace('schema_version="1"', 'schema_version="2"'),
    lambda text: '<!DOCTYPE prompt_matrix [<!ENTITY x "bad">]>' + text,
    lambda text: text + 'x' * MAX_MATRIX_BYTES,
    lambda text: text[:-30],
])
def test_invalid_or_incomplete_matrices_are_rejected(transform):
    with pytest.raises(ValueError):
        validate_matrix(transform(policy()))


@pytest.mark.parametrize('url', [
    'http://docs.google.com/document/d/example_document_id/edit',
    'https://docs.google.com.evil.test/document/d/example_document_id/edit',
    'https://user:password@docs.google.com/document/d/example_document_id/edit',
    'https://docs.google.com:8443/document/d/example_document_id/edit',
    'https://127.0.0.1/document/d/example_document_id/edit',
    'https://docs.google.com/document/d/e/published-id/pub',
    'https://docs.google.com/spreadsheets/d/example_document_id/edit',
])
def test_only_normal_https_google_doc_links_are_accepted(url):
    with pytest.raises(ValueError):
        google_doc_id(url)


def test_google_export_uses_plain_text_and_does_not_forward_bearer_on_redirect():
    public = fake_connection('\ufeff' + policy())
    with patch('ledger.prompt_matrix.http.client.HTTPSConnection', return_value=public) as connect:
        assert fetch_google_doc(DOC) == policy()
    assert connect.call_args.args[0] == 'docs.google.com'
    assert public.request.call_args.args == ('GET', '/document/d/example_document_id/export?format=txt')
    assert 'Authorization' not in public.request.call_args.kwargs['headers']
    first = fake_connection('', status=302, location='https://download.googleusercontent.com/export.txt')
    second = fake_connection(policy())
    with patch('ledger.prompt_matrix.http.client.HTTPSConnection', side_effect=[first, second]) as connect:
        assert fetch_google_doc(DOC, 'private-token') == policy()
    assert connect.call_args_list[0].args[0] == 'www.googleapis.com'
    assert first.request.call_args.args[1] == '/drive/v3/files/example_document_id/export?mimeType=text%2Fplain'
    assert first.request.call_args.kwargs['headers']['Authorization'] == 'Bearer private-token'
    assert 'Authorization' not in second.request.call_args.kwargs['headers']
    assert first.close.called and second.close.called


@pytest.mark.parametrize('connection', [
    fake_connection('', status=403), fake_connection('<html>Sign in</html>', content_type='text/html'),
    fake_connection(b'\xff'), fake_connection(b'x' * (MAX_MATRIX_BYTES + 1)),
    fake_connection('', status=302, location='http://localhost/secret'),
    fake_connection('', status=302, location='https://accounts.google.com/login'),
])
def test_bad_export_responses_fail_without_following_unapproved_hosts(connection):
    with patch('ledger.prompt_matrix.http.client.HTTPSConnection', return_value=connection) as connect:
        with pytest.raises((ValueError, UnicodeError)):
            fetch_google_doc(DOC)
    assert connect.call_count == 1


def test_export_timeout_and_redirect_limit():
    conn = fake_connection('')
    conn.connect.side_effect = TimeoutError('no network')
    with patch('ledger.prompt_matrix.http.client.HTTPSConnection', return_value=conn), pytest.raises(TimeoutError):
        fetch_google_doc(DOC)
    conn = fake_connection('', status=302, location='https://docs.google.com/again')
    with patch('ledger.prompt_matrix.http.client.HTTPSConnection', return_value=conn) as connect, pytest.raises(ValueError, match='redirects'):
        fetch_google_doc(DOC)
    assert connect.call_count == 4


def test_refresh_loads_once_per_revision_and_keeps_last_valid_or_bundled(caplog):
    matrix = PromptMatrix(DOC, 'DO_NOT_LOG_TOKEN')
    with patch('ledger.prompt_matrix.fetch_google_doc', side_effect=[TimeoutError('DO_NOT_LOG_TOKEN'), policy('2'), '<html>bad</html>', policy('3')]) as fetch:
        assert matrix.refresh('startup')['outcome'] == 'bundled_fallback'
        assert matrix.refresh('startup')['outcome'] == 'bundled_fallback'
        assert fetch.call_count == 1
        assert matrix.refresh('reload-1')['version'] == '2'
        before = matrix.snapshot()
        assert matrix.refresh('reload-2')['outcome'] == 'retained'
        assert matrix.snapshot() == before
        assert matrix.refresh('reload-3')['version'] == '3'
    assert 'DO_NOT_LOG_TOKEN' not in caplog.text and DOC not in caplog.text
    assert 'DO_NOT_LOG_TOKEN' not in str(matrix.snapshot())


def test_matrix_is_system_policy_and_reserved_deliveries_survive_reload(joined):
    ledger, store, _, composer, api, slack = joined
    ledger.join(str(oid(10)))
    composer.matrix = PromptMatrix(DOC)
    composer.choose = lambda cs: cs[0]
    with patch('ledger.prompt_matrix.fetch_google_doc', side_effect=[policy('2'), policy('3')]) as fetch:
        api.complete.side_effect = RuntimeError('process crash after reservation')
        with pytest.raises(RuntimeError):
            message(joined, 'matrix-reserved')
        job = store.get('ledger_outbox', 'matrix-reserved')
        assert job['prompt_selection']['matrix']['version'] == '2'
        first_prompt = api.complete.call_args
        Worker(ledger, composer, slack).admin_command(str(oid(10)), ['reload-prompts'], 'reload-one')
        assert composer.matrix.snapshot()['version'] == '3'
        api.complete.side_effect = None
        Worker(ledger, composer, slack).outbox(claim(store, 'matrix-reserved'))
        assert api.complete.call_args == first_prompt
        old = store.get('ledger_outbox', 'matrix-reserved')['composed']
        new = message(joined, 'matrix-new', audience='shared')
        assert old['matrix_version'] == '2' and new['matrix_version'] == '3'
        assert old['matrix_sha256'] != new['matrix_sha256']
        system = api.complete.call_args.args[0][0]['content']
        assert policy('3') in system and PERSONA in system
        assert system.index('Application guardrails') > system.index('</prompt_matrix>')
        assert 'shared private Ledger channel' in system
        assert fetch.call_count == 2


def test_reload_marker_is_seen_by_other_workers_and_restricted_to_admin_board(joined):
    ledger, store, sources, composer, api, slack = joined
    ledger.join(str(oid(11)))
    second = Composer(store, api, matrix=PromptMatrix(DOC))
    composer.matrix = PromptMatrix(DOC)
    worker = Worker(ledger, composer, slack)
    with patch('ledger.prompt_matrix.fetch_google_doc', return_value=policy('2')) as fetch:
        composer.refresh_matrix()
        second.refresh_matrix()
        sources.data['members'][1]['role'] = 'resource_manager'
        for actor in (1, 2):
            with pytest.raises(Denied):
                worker.admin_command(str(oid(actor)), ['reload-prompts'], 'denied')
        assert not store.get('ledger_catalog', 'prompt_matrix_reload')
        worker.admin_command(str(oid(11)), ['reload-prompts'], 'board-reload')
        assert fetch.call_count == 3
        second.refresh_matrix()
        assert fetch.call_count == 4
        second.refresh_matrix()
        assert fetch.call_count == 4


def test_environment_configuration_and_cli_check_need_no_database(capsys):
    from ledger.cli import main
    settings = {'LEDGER_PROMPT_MATRIX_DOC_URL': DOC, 'LEDGER_PROMPT_MATRIX_GOOGLE_ACCESS_TOKEN': 'oauth-token'}
    with patch.dict('os.environ', settings, clear=True), patch('sys.argv', ['ledger', 'prompt-matrix']), \
         patch('ledger.prompt_matrix.fetch_google_doc', return_value=policy('2')) as fetch, patch('ledger.cli.dependencies') as dependencies:
        main()
    fetch.assert_called_once_with(DOC, 'oauth-token')
    dependencies.assert_not_called()
    assert '"version": "2"' in capsys.readouterr().out


def test_unconfigured_matrix_never_fetches_google_and_canned_fallback_still_works(joined):
    _, _, _, composer, api, _ = joined
    api.complete.side_effect = TimeoutError()
    with patch('ledger.prompt_matrix.fetch_google_doc') as fetch:
        result = composer.compose('kudos', 'nonparticipant', EXAMPLE_FACTS)
    assert result['outcome'] == 'fallback' and result['matrix_source'] == 'bundled'
    fetch.assert_not_called()


def test_older_uncomposed_reservations_receive_one_matrix_snapshot(joined):
    ledger, store, _, composer, api, slack = joined
    api.complete.side_effect = RuntimeError('stop after reservation')
    with pytest.raises(RuntimeError):
        message(joined, 'legacy-reservation')
    job = store.get('ledger_outbox', 'legacy-reservation')
    del job['prompt_selection']['matrix']
    store.put('ledger_outbox', job)
    voice = job['prompt_selection']['template']['variations'][0]['id']
    api.complete.side_effect = None
    Worker(ledger, composer, slack).outbox(claim(store, job['_id']))
    saved = store.get('ledger_outbox', job['_id'])
    assert saved['prompt_selection']['matrix']['sha256'] == saved['composed']['matrix_sha256']
    assert saved['composed']['prompt_variation'] == voice
