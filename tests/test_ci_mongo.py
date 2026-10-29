"""Exercise startup failure handling without Docker or a live database."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")


def shell_path(path):
    path = Path(path).resolve()
    return "/" + path.drive[0].lower() + path.as_posix()[2:] if os.name == "nt" else str(path)


@pytest.mark.skipif(not BASH, reason="Bash is required")
@pytest.mark.parametrize("mode", ["success", "exited", "ping_timeout", "primary_timeout", "init_failure", "run_failure"])
def test_mongo_startup_lifecycle(tmp_path, mode):
    calls = tmp_path / "calls"
    hook = tmp_path / "docker.sh"
    hook.write_text(r'''docker() {
    printf '%s\n' "$*" >> "$CALLS"
    case "$1" in
        run) [[ "$MODE" != run_failure ]] || return 17 ;;
        inspect)
            if [[ "$3" == '{{.State.Running}}' ]]; then
                if [[ "$MODE" == exited ]]; then printf false; else printf true; fi
            else
                printf '{"ExitCode":42,"OOMKilled":false}\n'
            fi ;;
        logs) printf 'mongod startup diagnostic\n' ;;
        exec)
            if [[ "$*" == *rs.initiate* && "$MODE" == init_failure ]]; then return 18; fi
            if [[ "$*" == *'ping:1'* && "$MODE" == ping_timeout ]]; then return 1; fi
            if [[ "$*" == *isWritablePrimary* && "$MODE" == primary_timeout ]]; then return 1; fi ;;
    esac
    return 0
}
sleep() { :; }
''', newline="\n")
    result = subprocess.run([BASH, shell_path(ROOT / "scripts/ci/start-mongo.sh")],
        env=dict(os.environ, BASH_ENV=shell_path(hook), CALLS=shell_path(calls), MODE=mode,
                 MONGO_TEST_IMAGE="mongo:test@sha256:test"),
        capture_output=True, text=True, timeout=20)
    trace = calls.read_text()
    if mode == "success":
        assert result.returncode == 0
        assert "rs.initiate" in trace and "isWritablePrimary" in trace
        assert "logs --timestamps" not in trace
    else:
        assert result.returncode != 0
        assert '"ExitCode":42' in result.stderr
        assert "mongod startup diagnostic" in result.stderr
        assert "logs --timestamps" in trace
        if mode in {"exited", "ping_timeout", "run_failure"}:
            assert "rs.initiate" not in trace
        if mode == "exited":
            assert "exec ledger-mongo" not in trace
        if mode == "run_failure":
            assert result.returncode == 17
        if mode == "init_failure":
            assert result.returncode == 18


def test_ci_lanes_are_independent_and_cleanup_follows_diagnostics():
    jobs = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())["jobs"]
    ordinary, mongo = jobs["test"], jobs["mongo-integration"]
    assert ordinary["runs-on"] == mongo["runs-on"] == "ubuntu-24.04"
    assert "needs" not in ordinary and "needs" not in mongo
    assert any('pytest -m "not mongo_integration"' in s.get("run", "") for s in ordinary["steps"])
    assert all("start-mongo" not in s.get("run", "") for s in ordinary["steps"])
    assert any("pytest -m mongo_integration" in s.get("run", "") for s in mongo["steps"])
    assert "@sha256:" in mongo["env"]["MONGO_TEST_IMAGE"]
    diagnostics, cleanup = mongo["steps"][-2:]
    assert diagnostics["if"] == "failure()" and "docker logs" in diagnostics["run"]
    assert cleanup["if"] == "always()" and "docker rm -fv" in cleanup["run"]
