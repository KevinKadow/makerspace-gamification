"""Static Rails extraction and refresh; no live Mongo, network, or Rails execution."""
import json
from hashlib import sha256
from datetime import datetime
import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUBY = shutil.which('ruby')
BASH = shutil.which('bash')
GIT = shutil.which('git')
EXTRACTOR = ROOT / 'scripts/anomaly/extract_schema.rb'


@pytest.fixture(scope='module')
def ruby():
    if not RUBY:
        pytest.skip('Ruby 3.4 with Prism and ActiveSupport is required (also present in Docker schema stage)')
    check = subprocess.run([RUBY, '-rprism', '-ractive_support/inflector', '-e', 'exit 0'],
                           capture_output=True, text=True, timeout=40)
    if check.returncode:
        pytest.skip('Ruby Prism/ActiveSupport unavailable; use Docker schema stage')
    return RUBY


def write_tree(root, files):
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='\n')


def extract(ruby, source):
    return subprocess.run([ruby, str(EXTRACTOR), '--root', str(source), '--commit', 'a' * 40],
                          capture_output=True, text=True, timeout=40)


def test_extracts_concerns_embedded_ids_enums_and_foreign_keys_without_execution(ruby, tmp_path):
    write_tree(tmp_path, {
        'app/models/concerns/shared_fields.rb': '''module SharedFields
  extend ActiveSupport::Concern
  included do
    field :expires_on, type: Date
  end
end
''',
        'app/models/member.rb': '''File.write('SHOULD_NOT_EXIST', 'executed')
class Member
  include Mongoid::Document
  include Mongoid::Timestamps
  include SharedFields
  BASE = %w[active pending].freeze
  STATUSES = (BASE + ['revoked']).freeze
  field :status, type: String, default: 'active'
  field :phone
  field :stored, type: String, as: :friendly
  field :groupName, type: String
  belongs_to :group, class_name: 'Group', foreign_key: 'groupName', primary_key: 'name', optional: true
  validates :status, inclusion: { in: STATUSES }
  validates :status, presence: true
  validates :friendly, presence: true
  validates :phone, presence: true, if: :active?
  validate :callback_must_not_execute
  def callback_must_not_execute
    raise 'Never execute a Rails method'
  end
end
''',
        'app/models/group.rb': '''class Group
  include Mongoid::Document
  field :name, type: String
  store_in collection: 'member_groups'
  embeds_many :items, class_name: 'GroupItem'
  index({name: 1}, unique: true, partial_filter_expression: {name: {'$type' => 'string'}})
end
''',
        'app/models/group_item.rb': '''class GroupItem
  include Mongoid::Document
  embedded_in :group
  field :value, type: Integer
end
''',
        'app/models/ticket.rb': '''class FixTicket
  include Mongoid::Document
  field :_id, type: FixTicketId, default: -> { raise 'Never execute a default' }
end
''',
        'config/initializers/inflections.rb': '# No custom inflections\n',
    })
    result = extract(ruby, tmp_path)
    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    collections = manifest['collections']
    assert set(collections) == {'members', 'member_groups', 'fix_tickets', 'checkins', 'notes'}
    members = collections['members']
    assert members['fields']['expires_on']['types'] == ['Date']
    assert members['fields']['created_at']['types'] == ['Time']
    assert members['fields']['status']['enum'] == ['active', 'pending', 'revoked']
    assert members['required'] == ['_id', 'status', 'stored']
    assert members['fields']['stored']['types'] == ['String']
    assert 'friendly' not in members['fields']
    assert members['fields']['phone']['types'] == ['Object']
    assert members['references'] == [{'field': 'groupName', 'collection': 'member_groups', 'optional': True, 'array': False, 'target_field': 'name'}]
    assert collections['member_groups']['fields']['items']['embedded']['fields']['value']['types'] == ['Integer']
    assert collections['fix_tickets']['fields']['_id']['types'] == ['Integer', 'BSON::ObjectId']
    assert collections['member_groups']['indexes'][0]['unique'] is True
    assert not (tmp_path / 'SHOULD_NOT_EXIST').exists()
    assert not (ROOT / 'SHOULD_NOT_EXIST').exists()
    assert manifest['provenance']['rails_commit'] == 'a' * 40
    assert set(d['code'] for d in manifest['diagnostics']) >= {'untyped_field', 'conditional_validation_not_executed', 'custom_validation_not_executed'}
    repeated = json.loads(extract(ruby, tmp_path).stdout)
    assert datetime.fromisoformat(manifest['provenance']['build_time']).tzinfo
    assert manifest['provenance']['contract_hash'] == sha256(json.dumps(collections, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    manifest['provenance'].pop('build_time')
    repeated['provenance'].pop('build_time')
    assert repeated == manifest


@pytest.mark.parametrize('declaration', [
    "store_in collection: -> { ENV.fetch('COLLECTION') }",
    'field dynamic_name, type: String',
    'field :value, type: UnreviewedCustomType',
    "if ENV['FLAG']; field :conditional, type: String; end",
    "class_eval('field :injected, type: String')",
    '[:computed].each { |name| field name, type: String }',
])
def test_structural_ambiguity_fails_without_evaluation(ruby, tmp_path, declaration):
    write_tree(tmp_path, {'app/models/item.rb': f'class Item\n include Mongoid::Document\n {declaration}\nend\n'})
    result = extract(ruby, tmp_path)
    assert result.returncode == 1
    assert 'Schema extraction failed' in result.stderr
    assert not result.stdout


def test_dynamic_fields_and_unknown_nested_payloads_remain_open(ruby, tmp_path):
    write_tree(tmp_path, {'app/models/event.rb': '''class Event
 include Mongoid::Document
 include Mongoid::Attributes::Dynamic
 field :payload, type: Hash
 field :tags, type: Array
end
'''})
    result = extract(ruby, tmp_path)
    assert result.returncode == 0, result.stderr
    contract = json.loads(result.stdout)['collections']['events']
    assert contract['dynamic'] is True
    assert contract['fields']['payload'] == {'types': ['Hash']}


def shell_path(path):
    path = Path(path).resolve()
    return '/' + path.drive[0].lower() + path.as_posix()[2:] if os.name == 'nt' else str(path)


@pytest.mark.skipif(not BASH, reason='Bash required for repository URL validation')
@pytest.mark.parametrize('remote', [
    'https://credential-sentinel@example.invalid/repo.git',
    'HTTP://user:credential-sentinel@example.invalid/repo.git',
    'https://example.invalid/repo.git?token=credential-sentinel',
    'ssh://git:credential-sentinel@example.invalid/repo.git',
    'ssh://git%3Acredential-sentinel@example.invalid/repo.git',
    'ssh://git@example.invalid/repo.git#credential-sentinel',
    'git:credential-sentinel@example.invalid:org/repo.git',
    'file:///repo.git?token=credential-sentinel',
    'http::https://credential-sentinel@example.invalid/repo.git',
])
def test_refresh_rejects_credential_urls_before_git_or_snapshot_without_echoing_secrets(tmp_path, remote):
    project = tmp_path / 'project'
    write_tree(project, {'scripts/anomaly/refresh-rails.sh': (ROOT / 'scripts/anomaly/refresh-rails.sh').read_text()})
    env = dict(os.environ, ANOMALY_RAILS_REPOSITORY=remote, ANOMALY_RAILS_REF='refs/heads/master', ANOMALY_RAILS_COMMIT='')
    result = subprocess.run([BASH, shell_path(project / 'scripts/anomaly/refresh-rails.sh')], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert 'credential-sentinel' not in result.stdout + result.stderr
    assert 'must omit credentials' in result.stderr
    assert not (project / 'build').exists()


@pytest.mark.skipif(not BASH, reason='Bash required for repository URL validation')
@pytest.mark.parametrize('remote', [
    'https://example.invalid/org/repo.git',
    'ssh://git@example.invalid/org/repo.git',
    'git@example.invalid:org/repo.git',
])
def test_refresh_allows_credential_helper_and_passwordless_ssh_addresses(tmp_path, remote):
    project = tmp_path / 'project'
    write_tree(project, {'scripts/anomaly/refresh-rails.sh': (ROOT / 'scripts/anomaly/refresh-rails.sh').read_text()})
    shim = tmp_path / 'no-network.sh'
    # Reaching Git proves validation accepted the URL; no test uses the network.
    shim.write_text('git() { exit 71; }\n', newline='\n')
    env = dict(os.environ, ANOMALY_RAILS_REPOSITORY=remote, ANOMALY_RAILS_REF='refs/heads/master',
               ANOMALY_RAILS_COMMIT='', BASH_ENV=shell_path(shim))
    result = subprocess.run([BASH, shell_path(project / 'scripts/anomaly/refresh-rails.sh')], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 71
    assert 'must omit credentials' not in result.stderr
    assert not (project / 'build/anomaly/rails').exists()


@pytest.mark.skipif(not (GIT and BASH), reason='Git and Bash are needed for isolated refresh tests')
def test_refreshes_latest_commit_without_touching_source_checkout_and_keeps_previous_on_failure(git_fixture_root):
    tmp_path = git_fixture_root
    upstream, project = tmp_path / 'upstream', tmp_path / 'project'
    write_tree(upstream, {
        'app/models/item.rb': 'class Item\n include Mongoid::Document\nend\n',
        'config/initializers/inflections.rb': '# none\n',
        'docs/mongodb-collections.MD': '# Collections\n',
        'Gemfile.lock': 'GEM\n',
    })
    def git(*args):
        return subprocess.run([GIT, '-c', 'user.name=Schema Test', '-c', 'user.email=schema@example.invalid',
                               '-C', str(upstream), *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-b', 'master')
    git('add', '.')
    git('commit', '-m', 'fixture')
    first = git('rev-parse', 'HEAD')
    write_tree(project, {'scripts/anomaly/refresh-rails.sh': (ROOT / 'scripts/anomaly/refresh-rails.sh').read_text()})
    script = project / 'scripts/anomaly/refresh-rails.sh'
    env = dict(os.environ, ANOMALY_RAILS_REPOSITORY=shell_path(upstream), ANOMALY_RAILS_REF='refs/heads/master', ANOMALY_RAILS_COMMIT='')
    def refresh(environment):
        return subprocess.run([BASH, shell_path(script)], cwd=tmp_path, env=environment,
                              capture_output=True, text=True, timeout=40)
    result = refresh(env)
    assert result.returncode == 0, result.stderr
    snapshot = project / 'build/anomaly/rails'
    assert (snapshot / 'rails.commit').read_text().strip() == first
    (upstream / 'app/models/item.rb').write_text('class Item\n include Mongoid::Document\n field :new_field, type: String\nend\n')
    git('add', '.')
    git('commit', '-m', 'new field')
    second = git('rev-parse', 'HEAD')
    (upstream / 'local-uncommitted.txt').write_text('leave me alone')
    assert refresh(env).returncode == 0
    assert (snapshot / 'rails.commit').read_text().strip() == second != first
    assert 'new_field' in (snapshot / 'app/models/item.rb').read_text()
    assert not (snapshot / 'local-uncommitted.txt').exists()
    assert (upstream / 'local-uncommitted.txt').read_text() == 'leave me alone'
    failed = refresh(dict(env, ANOMALY_RAILS_REF='missing-ref'))
    assert failed.returncode != 0
    assert (snapshot / 'rails.commit').read_text().strip() == second
    pinned = refresh(dict(env, ANOMALY_RAILS_COMMIT=first))
    assert pinned.returncode == 0, pinned.stderr
    assert (snapshot / 'rails.commit').read_text().strip() == first
    assert 'new_field' not in (snapshot / 'app/models/item.rb').read_text()
    invalid = refresh(dict(env, ANOMALY_RAILS_COMMIT='HEAD~1'))
    assert invalid.returncode == 2
    assert (snapshot / 'rails.commit').read_text().strip() == first


@pytest.fixture
def git_fixture_root(tmp_path):
    yield tmp_path
    # Git marks object files read-only on Windows; let the repository's normal
    # session cleanup remove ONLY this test's disposable fixture tree.
    if os.name == 'nt':
        for path in tmp_path.rglob('*'):
            if path.is_file() and not path.is_symlink():
                path.chmod(path.stat().st_mode | stat.S_IWRITE)
