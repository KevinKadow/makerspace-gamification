"""Anomaly deployment boundaries without Docker, database, or external services."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash')


def test_standalone_stack_separates_metadata_databases_and_inference_credentials():
    config = yaml.safe_load((ROOT / 'compose.anomaly.yaml').read_text())
    services = config['services']
    assert set(services) == {'ledger-anomaly', 'anomaly-postgres', 'metabase-anomaly', 'metabase-raw'}
    anomaly = services['ledger-anomaly']
    assert anomaly['build']['dockerfile'] == 'Dockerfile.anomaly'
    assert set(anomaly['depends_on']) == {'anomaly-postgres'}
    assert anomaly['healthcheck']['test'] == ['CMD', 'python', '-m', 'ledger.anomaly', 'ready']
    env = anomaly['environment']
    assert env['ANOMALY_SCHEMA_PATH'] == '/app/schema/manifest.json'
    assert env['ANOMALY_REPORTING_DB_USER'] == '${ANOMALY_REPORTING_DB_USER:-anomaly_reporter}'
    assert env['ANOMALY_REPORTING_DB_NAME'] == '${ANOMALY_REPORTING_DB_NAME:-ledger_anomaly}'
    assert env['ANOMALY_REPORTING_DB_HOST'] == '${ANOMALY_REPORTING_DB_HOST:-anomaly-postgres}'
    assert env['MQTT_CLIENT_ID'] == '${ANOMALY_MQTT_CLIENT_ID:-ledger-anomaly}'
    assert 'ANOMALY_STATE_DIR' not in env
    assert env['ANOMALY_METABASE_PUBLIC_URL'] == '${ANOMALY_METABASE_PUBLIC_URL:-${METABASE_ANOMALY_PUBLIC_URL:-http://localhost:3001}}'
    assert 'ANOMALY_METABASE_BOOTSTRAP_API_KEY' not in env
    assert 'ANOMALY_METABASE_RAW_API_KEY' not in env
    assert 'ANOMALY_POSTGRES_PASSWORD' not in env
    assert not services['anomaly-postgres'].get('ports')
    for name, database in [('metabase-anomaly', 'metabase_anomaly'), ('metabase-raw', 'metabase_raw')]:
        service = services[name]
        assert service['build']['context'] == '${METABASE_SOURCE_DIR:-../metabase}'
        assert service['build']['args']['VERSION'] == '"${METABASE_VERSION:-v0.0.0-anomaly}"'
        assert service['environment']['MB_DB_DBNAME'] == database
        assert service['environment']['MB_DB_USER'] == database
        assert 'MLAB_URI' not in service['environment']  # Explicit API bootstrap, never mistaken for MB_DB_URI.
        assert 'LEDGER_LLM_API_KEY' not in service['environment']
        assert service['environment']['MB_ENABLE_PUBLIC_SHARING'] == 'false'
        assert service['ports'][0].startswith('127.0.0.1:')
    assert services['metabase-anomaly']['environment']['MB_ENCRYPTION_SECRET_KEY'] != services['metabase-raw']['environment']['MB_ENCRYPTION_SECRET_KEY']
    curated = services['metabase-anomaly']['environment']
    assert curated['MB_ANOMALY_ANALYTICS_ONLY'] == 'true'
    assert curated['MB_METABOT_QUERY_EXECUTION_ENABLED'] == 'true'
    assert curated['MB_METABOT_SQL_EXECUTION_ENABLED'] == 'false'
    assert curated['MB_LLM_ALLOWED_NETWORKS'] == 'allow-private'
    assert 'MB_ANOMALY_ANALYTICS_DATABASE_IDS' not in curated
    assert services['metabase-raw']['environment']['MB_AI_FEATURES_ENABLED'] == 'false'
    assert not any(key.startswith('MB_LLM_') for key in services['metabase-raw']['environment'])
    assert env['ANOMALY_SLACK_ALERT_MODE'] == '${ANOMALY_SLACK_ALERT_MODE:-shadow}'
    assert env['ANOMALY_SYSLOG_TRANSPORT'] == '${ANOMALY_SYSLOG_TRANSPORT:-tls}'
    assert env['MLAB_DATABASE'] == '${MLAB_DATABASE:-}'
    assert set(config['volumes']) == {'anomaly-postgres'}


def test_schema_stage_has_no_rails_execution_and_postgres_reporter_has_no_blanket_select():
    dockerfile = (ROOT / 'Dockerfile.anomaly').read_text()
    assert 'FROM ruby:3.4.4-slim AS schema' in dockerfile
    assert 'extract_schema.rb --root /rails' in dockerfile
    assert 'COPY --from=schema /schema/manifest.json /app/schema/manifest.json' in dockerfile
    assert 'bundle exec' not in dockerfile and 'rails runner' not in dockerfile
    assert "'.[anomaly]'" in dockerfile
    init = (ROOT / 'deployment/postgres/01-anomaly-databases.sh').read_text()
    assert 'ALTER ROLE anomaly_reporter SET default_transaction_read_only = on;' in init
    assert 'CREATE DATABASE ledger_anomaly OWNER anomaly_writer;' in init
    assert 'CREATE DATABASE metabase_anomaly OWNER metabase_anomaly;' in init
    assert 'CREATE DATABASE metabase_raw OWNER metabase_raw;' in init
    assert 'GRANT SELECT ON ALL' not in init
    assert 'NOSUPERUSER NOCREATEDB NOCREATEROLE' in init
    assert '\\getenv anomaly_password ANOMALY_DB_PASSWORD' in init


def shell_path(path):
    path = Path(path).resolve()
    return '/' + path.drive[0].lower() + path.as_posix()[2:] if os.name == 'nt' else str(path)


@pytest.mark.skipif(not BASH, reason='Bash required for lifecycle tests')
@pytest.mark.parametrize('failure', ['', 'refresh', 'preflight'])
def test_anomaly_rebuild_preflights_before_shutdown_and_checks_standalone_ready(tmp_path, failure):
    project = tmp_path / 'deployment'
    scripts = project / 'scripts'
    (scripts / 'anomaly').mkdir(parents=True)
    (scripts / 'rebuild.sh').write_text((ROOT / 'scripts/rebuild.sh').read_text(), newline='\n')
    (scripts / 'anomaly/refresh-rails.sh').write_text('''#!/usr/bin/env bash
printf 'refresh\\n' >> "$TEST_LOG"
if [[ "$TEST_FAILURE" == refresh ]]; then exit 19; fi
mkdir -p build/anomaly
''', newline='\n')
    shim = tmp_path / 'docker.sh'
    shim.write_text('''docker() {
    printf '%s\\n' "$*" >> "$TEST_LOG"
    if [[ "$1" == info ]]; then return 0; fi
    shift 3
    if [[ "${2:-}" == --help ]]; then printf '%s\\n' '--wait-timeout --ignore-buildable'; return 0; fi
    if [[ "$1 ${2:-}" == 'config --services' ]]; then printf 'ledger-anomaly\\nanomaly-postgres\\nmetabase-anomaly\\nmetabase-raw\\n'; return 0; fi
    if [[ "$*" == *'/app/schema/manifest.json'* ]]; then printf '%s\\n' '{"format_version":1,"collections":{"members":{}}}'; return 0; fi
    if [[ "$*" == *'preflight --source-only'* && "$TEST_FAILURE" == preflight ]]; then return 19; fi
    return 0
}
''', newline='\n')
    log = tmp_path / 'calls.log'
    env = dict(os.environ, BASH_ENV=shell_path(shim), TEST_LOG=shell_path(log), TEST_FAILURE=failure)
    result = subprocess.run([BASH, shell_path(scripts / 'rebuild.sh')], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    calls = log.read_text().splitlines()
    if failure:
        assert result.returncode == 19, result.stderr
        assert not any(' down ' in call for call in calls)
        assert 'Existing containers have not been stopped' in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        refresh = calls.index('refresh')
        build = next(i for i, call in enumerate(calls) if ' build ' in call)
        preflight = next(i for i, call in enumerate(calls) if 'preflight --source-only' in call)
        down = next(i for i, call in enumerate(calls) if ' down ' in call)
        assert refresh < build < preflight < down
        assert any('exec -T ledger-anomaly python -m ledger.anomaly ready' in call for call in calls)
        assert not any('exec -T ledger-web' in call for call in calls)
        assert json.loads((project / 'build/anomaly/manifest.json').read_text())['format_version'] == 1
    if failure == 'preflight':
        assert (project / 'build/anomaly/manifest.json').exists()  # Permission operator can inspect the exact contract.
