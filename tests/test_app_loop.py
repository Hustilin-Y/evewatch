from pathlib import Path

import pytest

from evewatch import app
from evewatch.config import default_config


class FakeQueue:
    def __init__(self):
        self.submitted = []
        self.muted = False
        self.started = False
        self.closed = False

    def prepare(self, specs):
        pass

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def set_muted(self, muted):
        self.muted = muted

    def submit(self, spec, priority, label=""):
        self.submitted.append((spec, priority, label))
        return True


class Broken:
    name = "broken"

    def __init__(self):
        self.ticks = 0
        self.closed = False

    def tick(self, now):
        self.ticks += 1
        raise RuntimeError("boom")

    def status(self):
        return "broken"

    def close(self):
        self.closed = True


class Counting:
    name = "counting"

    def __init__(self):
        self.ticks = 0

    def tick(self, now):
        self.ticks += 1

    def status(self):
        return "counting"


@pytest.fixture
def harness(monkeypatch, tmp_path):
    queue = FakeQueue()
    monkeypatch.setattr(app, "make_queue", lambda config, silent=False: queue)
    monkeypatch.setattr(app, "TICK_SEC", 0)
    monkeypatch.setattr(app, "enable_dpi_awareness", lambda: None)
    cfg = default_config(tmp_path)
    return queue, cfg


def keys_then_quit(monkeypatch, script):
    steps = iter(script)

    def poll():
        try:
            return next(steps)
        except StopIteration:
            return ["q"]

    monkeypatch.setattr(app, "_poll_keys", poll)


def test_fault_signal_after_repeated_errors_and_loop_survives(harness, monkeypatch):
    queue, cfg = harness
    broken = Broken()
    monkeypatch.setattr(app, "build_watchers", lambda config, q: [broken])
    keys_then_quit(monkeypatch, [[]] * 12)
    assert app.run(cfg) == 0
    assert broken.ticks == 12  # an exception never stopped the loop
    faults = [s for s in queue.submitted if s[2] == "fault"]
    assert len(faults) == 1 and faults[0][0] == cfg.sounds.fault_sound
    assert broken.closed and queue.closed


def test_pause_stops_ticks_and_mute_reaches_queue(harness, monkeypatch):
    queue, cfg = harness
    watcher = Counting()
    monkeypatch.setattr(app, "build_watchers", lambda config, q: [watcher])
    keys_then_quit(monkeypatch, [[], ["p"], [], [], ["p"], ["m"], []])
    assert app.run(cfg) == 0
    # iteration 1 ticks; 2-4 are paused ("p" is handled before the tick); 5 un-pauses and ticks; 6 and 7 tick
    assert watcher.ticks == 4
    assert queue.muted is True


def test_status_key_does_not_crash_without_watchers(harness, monkeypatch, capsys):
    queue, cfg = harness
    monkeypatch.setattr(app, "build_watchers", lambda config, q: [])
    keys_then_quit(monkeypatch, [["s"]])
    assert app.run(cfg) == 0
    out = capsys.readouterr().out
    assert "no watchers active" in out
    assert queue.started and queue.closed


def test_keyboard_interrupt_exits_cleanly(harness, monkeypatch):
    queue, cfg = harness
    monkeypatch.setattr(app, "build_watchers", lambda config, q: [])

    def boom():
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "_poll_keys", boom)
    assert app.run(cfg) == 0
    assert queue.closed


def test_uncalibrated_region_builds_no_local_watcher(tmp_path):
    cfg = default_config(Path(tmp_path))
    assert app.build_watchers(cfg, FakeQueue()) == []


class Closable(Counting):
    def __init__(self):
        super().__init__()
        self.closed = False

    def close(self):
        self.closed = True


def test_config_change_on_disk_reloads_watchers(harness, monkeypatch, tmp_path):
    import dataclasses

    from evewatch.config import default_config, save_config

    queue, cfg = harness
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    monkeypatch.setattr(app, "CONFIG_POLL_SEC", 0)
    built = []

    def build(config, q):
        watcher = Closable()
        built.append((config, watcher))
        return [watcher]

    monkeypatch.setattr(app, "build_watchers", build)
    step = {"n": 0}

    def poll():
        step["n"] += 1
        if step["n"] == 3:
            changed = dataclasses.replace(cfg, local=dataclasses.replace(cfg.local, region=(1, 2, 30, 400)))
            save_config(changed, path)
        return ["q"] if step["n"] >= 8 else []

    monkeypatch.setattr(app, "_poll_keys", poll)
    assert app.run(cfg, path) == 0
    assert len(built) == 2
    assert built[0][1].closed and built[1][1].closed
    assert built[1][0].local.region == (1, 2, 30, 400)


def test_invalid_config_keeps_running_with_old_watchers(harness, monkeypatch, tmp_path, capsys):
    from evewatch.config import save_config

    queue, cfg = harness
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    monkeypatch.setattr(app, "CONFIG_POLL_SEC", 0)
    built = []

    def build(config, q):
        watcher = Closable()
        built.append(watcher)
        return [watcher]

    monkeypatch.setattr(app, "build_watchers", build)
    step = {"n": 0}

    def poll():
        step["n"] += 1
        if step["n"] == 2:
            path.write_text("[local]\nfps = \"fast\"\n", encoding="utf-8")
        return ["q"] if step["n"] >= 6 else []

    monkeypatch.setattr(app, "_poll_keys", poll)
    assert app.run(cfg, path) == 0
    assert len(built) == 1 and built[0].ticks >= 4
    assert "config not reloaded" in capsys.readouterr().out


def test_region_warning_for_whole_window_selection():
    from evewatch.local_watch import region_warning

    assert region_warning((427, 41, 311, 550)) is not None
    assert "narrow column" in region_warning((427, 41, 311, 550))
    assert region_warning((640, 103, 14, 488)) is None
