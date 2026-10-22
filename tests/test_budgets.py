"""Resource and latency budgets from the spec, section 10: zero network, bounded memory, <= 1.5 s latency."""

import gc
import random
import socket
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from helpers import BG, BLUE, GREY, ORANGE, RED, local_cfg, make_frame
from test_universe import make_universe
from evewatch import app
from evewatch.config import TierConfig, default_config, save_config
from evewatch.intel_watch import ChatlogTailer, IntelWatcher
from evewatch.local_watch import LocalWatcher
from evewatch.sounds import SoundLibrary, SoundQueue
import dataclasses


class Backend:
    def play(self, sound):
        return True

    def stop(self):
        pass


# ---------------------------------------------------------------- no network at all in `run`
def test_run_makes_no_network_calls(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "Local_20261013_100000_1.txt").write_bytes(
        b"\xff\xfe" + ("﻿[ 2026.10.13 10:00:01 ] EVE System > Channel changed to Local : Obe\r\n").encode("utf-16-le")
    )
    (logs / "Intel_20261013_100000_1.txt").write_bytes(b"\xff\xfe")
    universe = tmp_path / "universe.json"
    make_universe().save(universe)
    cfg = default_config(tmp_path)
    cfg = dataclasses.replace(
        cfg,
        local=dataclasses.replace(cfg.local, region=(0, 0, 14, 120)),
        intel=dataclasses.replace(
            cfg.intel, chatlog_dir=str(logs), channels=("Intel",), universe_file=universe, tiers=(TierConfig(5, "auto:intel_5"),)
        ),
    )
    config_path = tmp_path / "config.toml"
    save_config(cfg, config_path)

    attempts = []

    def forbid(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("network access attempted")

    class NoSocket:
        def __init__(self, *a, **k):
            forbid(*a)

    monkeypatch.setattr(socket, "socket", NoSocket)
    monkeypatch.setattr(socket, "create_connection", forbid)
    monkeypatch.setattr(socket, "getaddrinfo", forbid)
    monkeypatch.setattr(socket, "gethostbyname", forbid)
    monkeypatch.setattr(app, "TICK_SEC", 0.02)
    monkeypatch.setattr(app, "enable_dpi_awareness", lambda: None)
    steps = {"n": 0}

    def poll():
        steps["n"] += 1
        return ["q"] if steps["n"] > 40 else []

    monkeypatch.setattr(app, "_poll_keys", poll)
    assert app.run(cfg, config_path, silent=True) == 0
    assert attempts == []
    assert steps["n"] > 40


# ---------------------------------------------------------------- bounded memory in an accelerated soak
def random_column(rng):
    icons = []
    y = 4
    for _ in range(rng.randint(0, 6)):
        icons.append((y, 12, rng.choice([RED, ORANGE, GREY, BLUE])))
        y += 18
    return make_frame(icons, height=120, width=16)


def test_accelerated_soak_has_no_memory_growth(tmp_path):
    import logging

    # pytest's log capture keeps every record in memory; the real app writes them to a rotating file
    logging.disable(logging.INFO)
    try:
        _soak(tmp_path)
    finally:
        logging.disable(logging.NOTSET)


def _soak(tmp_path):
    rng = random.Random(7)
    local = local_cfg()
    emitted = []
    queue = SoundQueue(Backend(), SoundLibrary(tmp_path / "cache", tmp_path), 5.0)
    queue.start()
    watcher = LocalWatcher(local, lambda: random_column(rng), queue.submit, "auto:blind")
    universe = make_universe()
    intel_cfg = dataclasses.replace(
        default_config(tmp_path).intel,
        tiers=(TierConfig(2, "auto:intel_2"), TierConfig(5, "auto:intel_5")),
        dedup_sec=0,
        max_message_age_sec=0,
    )
    intel = IntelWatcher(intel_cfg, universe, [], lambda: "Obe", lambda *a: emitted.append(a))
    names = list(universe.systems.values())
    clock = 0.0

    def step(n):
        nonlocal clock
        for _ in range(n):
            clock += 0.5
            watcher.process_frame(random_column(rng), clock)
            system = rng.choice(names).name
            suffix = rng.choice(["", " clr", " +3", " nv"])
            intel.handle_line(f"﻿[ 2026.10.13 10:00:00 ] Pilot > {system}*{suffix}", clock)
            if rng.random() < 0.05:
                watcher.on_system_change(rng.choice(names).name)

    step(1500)  # warm-up: caches, queue thread, compiled regex
    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.take_snapshot()
    step(6000)
    gc.collect()
    growth = sum(s.size_diff for s in tracemalloc.take_snapshot().compare_to(baseline, "filename"))
    tracemalloc.stop()
    queue.close()
    assert growth < 1_500_000, f"memory grew by {growth / 1e6:.2f} MB over 6000 iterations"
    assert len(intel._last_seen) <= 2 * len(universe)
    assert queue.pending() < 50


# ---------------------------------------------------------------- latency
def test_local_signal_latency_is_within_budget():
    """Icon appears -> alert within 1.5 s at 2 fps with 2 confirm frames (spec section 10)."""
    cfg = local_cfg(fps=2, confirm_frames=2)
    state = {"frame": make_frame([])}
    emitted = []
    watcher = LocalWatcher(cfg, lambda: state["frame"], lambda *a: emitted.append(clock[0]), "auto:blind")
    clock = [0.0]
    appear_at = 3.04  # deliberately not aligned with the capture tick
    worst = 0.0
    for trial in range(10):
        emitted.clear()
        appear = appear_at + trial * 0.07
        clock[0] = 0.0
        state["frame"] = make_frame([])
        shown = False
        while clock[0] < appear + 3 and not emitted:
            if not shown and clock[0] >= appear:
                state["frame"] = make_frame([(10, 12, RED)])
                shown = True
            watcher.tick(clock[0])
            clock[0] += 0.1  # the app loop ticks every 0.1 s
        assert emitted, "no alert"
        worst = max(worst, emitted[0] - appear)
        watcher = LocalWatcher(cfg, lambda: state["frame"], lambda *a: emitted.append(clock[0]), "auto:blind")
        # start of a trial: baseline consumed in the first frames
    assert worst <= 1.5, f"worst-case latency {worst:.2f} s"


def test_intel_signal_latency_is_within_budget(tmp_path):
    from test_tailer import append, make_log

    path = make_log(tmp_path, "Intel", "20261013_100000")
    tailer = ChatlogTailer(tmp_path, "Intel")
    cfg = dataclasses.replace(default_config(tmp_path).intel, tiers=(TierConfig(5, "auto:intel_5"),), dedup_sec=0)
    seen = []
    watcher = IntelWatcher(cfg, make_universe(), [tailer], lambda: "Obe", lambda *a: seen.append(time.monotonic()))
    watcher.tick(0.0)  # attach at the end of the file
    stamp = datetime.now(timezone.utc).strftime("%Y.%m.%d %H:%M:%S")
    started = time.monotonic()
    append(path, f"﻿[ {stamp} ] Pilot > 4-HWWF*  camp\r\n")
    while not seen and time.monotonic() - started < 2.0:
        watcher.tick(time.monotonic())
        time.sleep(0.1)  # the app loop period
    assert seen, "no signal"
    assert seen[0] - started <= 1.5
