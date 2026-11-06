"""Exercise deployment failure ordering with a fake Docker CLI; never restart services."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash')
pytestmark = pytest.mark.skipif(not BASH, reason='Bash is required for deployment-script checks')


def shell_path(path):
    path = Path(path).resolve()
    if os.name == 'nt':
        return '/' + path.drive[0].lower() + path.as_posix()[2:]
    return str(path)


@pytest.fixture
def rebuild(tmp_path):
    log = tmp_path / 'docker.log'
    environment_file = tmp_path / 'fake-docker.sh'
    environment_file.write_text('''docker() {
    printf '%s\\t' "$PWD" "$@" >> "$DOCKER_TEST_LOG"
    printf '\\n' >> "$DOCKER_TEST_LOG"
    if [[ "$1" == info ]]; then return 0; fi
    shift 3  # compose --project-directory ROOT
    if [[ "${2:-}" == --help ]]; then
        printf '%s\\n' '--wait-timeout --ignore-buildable'
        return 0
    fi
    if [[ "$1" == "$DOCKER_TEST_FAIL" ]]; then return 19; fi
    return 0
}
''', encoding='utf-8', newline='\n')

    def run(*args, fail=''):
        if log.exists():
            log.unlink()
        env = dict(os.environ, BASH_ENV=shell_path(environment_file),
                   DOCKER_TEST_LOG=shell_path(log), DOCKER_TEST_FAIL=fail)
        result = subprocess.run([BASH, shell_path(ROOT / 'scripts/rebuild.sh'), *args],
                                cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
        calls = [line.rstrip('\t').split('\t') for line in log.read_text().splitlines()] if log.exists() else []
        # Every Docker call must resolve the same checkout, even from a different cwd.
        if calls:
            assert all(call[0] == calls[0][0] for call in calls)
            assert calls[0][0].endswith('/' + ROOT.name)
        compose_calls = [call[4:] for call in calls if call[1] == 'compose']
        return result, compose_calls

    return run


def test_rebuild_recreates_full_stack_after_images_and_checks_readiness(rebuild):
    result, calls = rebuild('--wait-timeout', '2400')
    assert result.returncode == 0, result.stderr
    lifecycle = [call for call in calls if '--help' not in call and call[0] != 'version']
    assert lifecycle[:4] == [['config', '--quiet'], ['build', '--pull', '--no-cache'],
                            ['pull', '--ignore-buildable'], ['down', '--remove-orphans', '--timeout', '60']]
    assert lifecycle[4] == ['up', '--detach', '--force-recreate', '--remove-orphans', '--no-build',
                            '--pull', 'never', '--wait', '--wait-timeout', '2400']
    assert lifecycle[5][:4] == ['exec', '-T', 'ledger-web', 'python']
    assert '/ready' in lifecycle[5][-1]
    assert lifecycle[6] == ['ps', '--all']
    assert 'readiness passed' in result.stdout
    assert all('--volumes' not in call and '-v' not in call and 'prune' not in call for call in calls)


@pytest.mark.parametrize('stage', ['config', 'build', 'pull'])
def test_preparation_failure_keeps_existing_services_running(rebuild, stage):
    result, calls = rebuild(fail=stage)
    assert result.returncode == 19
    assert not any(call[0] == 'down' or (call[0] == 'up' and '--help' not in call) for call in calls)
    assert 'Existing containers have not been stopped' in result.stderr


@pytest.mark.parametrize('stage', ['down', 'up', 'exec'])
def test_restart_failure_returns_nonzero_and_shows_status(rebuild, stage):
    result, calls = rebuild(fail=stage)
    assert result.returncode == 19
    assert calls[-1] == ['ps', '--all']
    assert 'readiness passed' not in result.stdout
    assert 'Rebuild failed during' in result.stderr


@pytest.mark.parametrize('args', [('--wait-timeout',), ('--wait-timeout', '0'),
                                  ('--wait-timeout', 'abc'), ('--unknown',)])
def test_invalid_options_fail_before_touching_docker(rebuild, args):
    result, calls = rebuild(*args)
    assert result.returncode == 2
    assert calls == []


def test_help_does_not_touch_docker(rebuild):
    result, calls = rebuild('--help')
    assert result.returncode == 0
    assert 'Usage:' in result.stdout
    assert calls == []
