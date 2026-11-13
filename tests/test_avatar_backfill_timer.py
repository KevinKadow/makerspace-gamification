from threading import Event
from unittest.mock import MagicMock

import pytest

from ledger import avatars, cli


@pytest.mark.parametrize("queue", ["avatars", "all"])
@pytest.mark.parametrize("fails", [False, True])
def test_worker_backfill_checks_at_most_every_five_minutes(env, monkeypatch, queue, fails):
    ledger, _, _, composer, _, slack = env
    stop = Event()
    clock = [0.0]
    ticks = iter([0.25, 299.75, 300.0, 300.25, 599.75, 600.0, 600.25])
    calls = []
    def reserve(_):
        calls.append(clock[0])
        if fails:
            raise OSError("Mongo temporarily unavailable")
    def step(*args, **kwargs):
        tick()
        return True
    def tick():
        try:
            clock[0] = next(ticks)
        except StopIteration:
            stop.set()
    class Stop:
        def is_set(self):
            return stop.is_set()
        def set(self):
            stop.set()
        def wait(self, seconds):
            tick()
            return stop.is_set()
    class Thread:
        def __init__(self, target, args, name, **kwargs):
            self.target, self.args, self.name = target, args, name
        def start(self):
            if self.name == "avatars":
                self.target(*self.args)
        def join(self, **kwargs):
            pass
    worker = MagicMock()
    worker.step.side_effect = step
    composer.refresh_matrix = MagicMock()
    monkeypatch.setattr(ledger.store, "ready", lambda: None, raising=False)
    monkeypatch.setattr(ledger.sources, "ready", lambda: None, raising=False)
    monkeypatch.setattr(cli, "dependencies", lambda: (ledger, composer, slack))
    monkeypatch.setattr(cli, "broker", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "Worker", lambda *args, **kwargs: worker)
    monkeypatch.setattr(cli, "Event", Stop)
    monkeypatch.setattr(cli, "Thread", Thread)
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(avatars, "backfill", reserve)
    monkeypatch.setenv("SLACK_BOT_USER_ID", "UBOT")
    monkeypatch.setattr("sys.argv", ["ledger", "worker", "--queue", queue])
    cli.main()
    assert calls == [0.0, 300.0, 600.0]
