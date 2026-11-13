"""Shared cleanup for direct, focused and CI pytest runs, including failures."""
import shutil
import warnings

import pytest


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    try:
        return (yield)
    finally:
        factory = getattr(session.config, "_tmp_path_factory", None)
        base = getattr(factory, "_basetemp", None)
        if base is not None:
            try:
                # Delete only the directory created/cleared by this session's
                # TempPathFactory. rmtree unlinks symlinks without following them.
                current = base.parent / "pytest-current"
                if current.is_symlink() and current.resolve() == base.resolve():
                    current.unlink()
                shutil.rmtree(base)
            except FileNotFoundError:
                pass
            except OSError as exc:
                warnings.warn(f"Could not clean pytest temporary directory {base}: {exc}")
                session.exitstatus = pytest.ExitCode.TESTS_FAILED
