"""Exercise the common harness after successful and unsuccessful sessions."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("body,code", [
    ("def test_example(tmp_path):\n    assert True\n", 0),
    ("def test_example(tmp_path):\n    assert False\n", 1),
    ("def test_example(tmp_path):\n    raise KeyboardInterrupt()\n", 2),
    ("raise RuntimeError('collection failed')\n", 2),
])
def test_session_removes_temporary_tree_on_all_exits(tmp_path, body, code):
    project = tmp_path / "project"
    project.mkdir()
    shutil.copyfile(Path(__file__).parents[1] / "conftest.py", project / "conftest.py")
    # Collection failures can leave a temp tree created by a collection plugin.
    with (project / "conftest.py").open("a") as f:
        f.write("\ndef pytest_sessionstart(session):\n    session.config._tmp_path_factory.getbasetemp()\n")
    (project / "test_example.py").write_text(body)
    base = tmp_path / ".pytest-run"
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "--basetemp", str(base)],
        cwd=project, capture_output=True, text=True, timeout=60)
    assert result.returncode == code, result.stdout + result.stderr
    assert not base.exists()


def test_cleanup_unlinks_dangling_and_external_symlinks(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    shutil.copyfile(Path(__file__).parents[1] / "conftest.py", project / "conftest.py")
    target = tmp_path / "external"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    (project / "test_example.py").write_text(
        "from pathlib import Path\n"
        "def test_example(tmp_path):\n"
        f"    tmp_path.joinpath('external').symlink_to(Path({str(target)!r}), target_is_directory=True)\n"
        "    tmp_path.joinpath('dangling').symlink_to(tmp_path / 'missing', target_is_directory=True)\n")
    base = tmp_path / ".pytest-links"
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "--basetemp", str(base)],
        cwd=project, capture_output=True, text=True, timeout=60)
    if "privilege" in result.stdout.lower() or "operation not permitted" in result.stdout.lower():
        pytest.skip("Host does not permit symlink creation")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not base.exists()
    assert (target / "keep.txt").read_text() == "keep"
