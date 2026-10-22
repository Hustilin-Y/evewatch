import numpy as np

from helpers import BG, GREY, RED, column, local_cfg, make_frame
from evewatch.local_watch import LocalStateMachine, LocalWatcher, replay_frames
from evewatch.sounds import BLIND_PRIORITY

T = {"terrible": 0, "bad": 0, "neutral": 0, "good": 0, "excellent": 0}


def counts(**kw):
    return {**T, **kw}


def feed(machine, state, t0=0.0, step=0.5, repeat=2):
    """Feeds a state `repeat` frames in a row; returns (alerts, next_time)."""
    alerts = []
    t = t0
    for _ in range(repeat):
        alerts += machine.update(state, t)
        t += step
    return alerts, t


def test_first_state_is_silent_baseline():
    m = LocalStateMachine(local_cfg())
    m.reset(arrival=False)
    alerts, _ = feed(m, counts(terrible=2))
    assert alerts == []
    assert m.stable == counts(terrible=2)


def test_growth_alerts_after_confirmation():
    m = LocalStateMachine(local_cfg())
    m.reset(arrival=False)
    _, t = feed(m, counts())
    assert m.update(counts(terrible=1), t) == []  # one frame is not enough
    second = m.update(counts(terrible=1), t + 0.5)
    assert [(a.class_name, a.reason) for a in second] == [("terrible", "grew")]


def test_flicker_does_not_alert():
    m = LocalStateMachine(local_cfg())
    m.reset(arrival=False)
    _, t = feed(m, counts())
    for i, c in enumerate([counts(bad=1), counts(), counts(bad=1), counts()]):
        assert m.update(c, t + i * 0.5) == []


def test_decrease_does_not_alert_but_updates_baseline():
    m = LocalStateMachine(local_cfg(cooldown_sec=0))
    m.reset(arrival=False)
    _, t = feed(m, counts(bad=2))
    alerts, t = feed(m, counts(bad=1), t)
    assert alerts == []
    alerts, _ = feed(m, counts(bad=2), t)
    assert [a.class_name for a in alerts] == ["bad"]


def test_highest_priority_class_wins():
    m = LocalStateMachine(local_cfg())
    m.reset(arrival=False)
    _, t = feed(m, counts())
    alerts, _ = feed(m, counts(neutral=1, bad=1, terrible=1), t)
    assert [a.class_name for a in alerts] == ["terrible"]


def test_growth_during_cooldown_is_deferred_until_it_ends():
    m = LocalStateMachine(local_cfg(cooldown_sec=3))
    m.reset(arrival=False)
    _, t = feed(m, counts())  # t = 1.0
    first = m.update(counts(terrible=1), t)
    first += m.update(counts(terrible=1), t + 0.5)
    assert [a.count for a in first] == [1]  # signalled at t = 1.5
    # a second red arrives one second later, inside the cooldown
    assert m.update(counts(terrible=2), 2.5) == []
    assert m.update(counts(terrible=2), 3.0) == []
    deferred = m.update(counts(terrible=2), 4.6)
    assert [(a.class_name, a.count, a.reason) for a in deferred] == [("terrible", 2, "grew")]
    assert m.update(counts(terrible=2), 5.0) == []  # not repeated


def test_deferred_growth_that_disappears_never_alerts():
    m = LocalStateMachine(local_cfg(cooldown_sec=10))
    m.reset(arrival=False)
    _, t = feed(m, counts())
    feed(m, counts(bad=1), t)
    feed(m, counts(bad=2), 5.0)
    feed(m, counts(bad=1), 6.0)
    assert m.update(counts(bad=1), 30.0) == []


def test_priority_loser_is_not_put_on_cooldown():
    m = LocalStateMachine(local_cfg(cooldown_sec=30))
    m.reset(arrival=False)
    _, t = feed(m, counts())
    alerts, t = feed(m, counts(terrible=1, neutral=1), t)
    assert [a.class_name for a in alerts] == ["terrible"]
    # a second neutral shortly after: its class never played, so no cooldown applies
    alerts, _ = feed(m, counts(terrible=1, neutral=2), t)
    assert [a.class_name for a in alerts] == ["neutral"]


def test_cooldown_is_per_class():
    m = LocalStateMachine(local_cfg(cooldown_sec=30))
    m.reset(arrival=False)
    _, t = feed(m, counts())
    first, t = feed(m, counts(bad=1), t)
    assert len(first) == 1
    second, _ = feed(m, counts(bad=1, terrible=1), t)
    assert [a.class_name for a in second] == ["terrible"]


def test_repeat_is_consistent_for_enemies_present_at_start():
    m = LocalStateMachine(local_cfg(repeat_while_present_sec=20))
    m.reset(arrival=False)
    quiet, _ = feed(m, counts(bad=1), 0.0)
    assert quiet == []  # silent baseline, no immediate "repeat"
    assert m.update(counts(bad=1), 10.0) == []
    assert [a.reason for a in m.update(counts(bad=1), 20.5)] == ["repeat"]


def test_disabled_class_never_alerts():
    m = LocalStateMachine(local_cfg())
    m.reset(arrival=False)
    _, t = feed(m, counts())
    alerts, _ = feed(m, counts(good=3, excellent=1), t)
    assert alerts == []


def test_arrival_alert_after_system_change():
    m = LocalStateMachine(local_cfg())
    m.reset(arrival=False)
    feed(m, counts())
    m.reset(arrival=True)
    alerts, _ = feed(m, counts(terrible=2, neutral=1), 10.0)
    assert [(a.class_name, a.reason, a.count) for a in alerts] == [("terrible", "arrival", 2)]


def test_arrival_silent_when_option_off():
    m = LocalStateMachine(local_cfg(alert_on_arrival=False))
    m.reset(arrival=True)
    alerts, _ = feed(m, counts(terrible=2))
    assert alerts == []


def test_repeat_while_present():
    m = LocalStateMachine(local_cfg(repeat_while_present_sec=20, cooldown_sec=0))
    m.reset(arrival=False)
    _, t = feed(m, counts())
    first, t = feed(m, counts(bad=1), t)
    assert len(first) == 1
    quiet, t = feed(m, counts(bad=1), t, step=5)
    assert quiet == []
    later = m.update(counts(bad=1), 30.0)
    assert [a.reason for a in later] == ["repeat"]


def test_no_repeat_by_default():
    m = LocalStateMachine(local_cfg())
    m.reset(arrival=False)
    _, t = feed(m, counts())
    feed(m, counts(bad=1), t)
    assert m.update(counts(bad=1), 500.0) == []


def make_watcher(local=None):
    emitted = []
    w = LocalWatcher(local or local_cfg(), lambda: make_frame([]), lambda *a: emitted.append(a), "auto:blind")
    return w, emitted


def test_watcher_emits_sound_and_priority():
    w, emitted = make_watcher()
    empty = make_frame([])
    with_enemy = make_frame(column(RED))
    for i, f in enumerate([empty, empty, with_enemy, with_enemy]):
        w.process_frame(f, i * 0.5)
    assert emitted == [("auto:terrible", 5, "local:terrible")]
    assert "ter:1" in w.status()


def test_watcher_system_change_resets_baseline():
    w, emitted = make_watcher()
    frame = make_frame(column(GREY))
    w.process_frame(frame, 0.0)
    w.process_frame(frame, 0.5)
    assert emitted == []
    w.on_system_change("Jita")
    w.process_frame(frame, 1.0)
    w.process_frame(frame, 1.5)
    assert emitted == [("auto:neutral", 3, "local:neutral")]


def test_watcher_blind_signal_suppresses_class_alerts():
    local = local_cfg(background_color=BG, blind_after_sec=2, blind_repeat_sec=10)
    w, emitted = make_watcher(local)
    ok = make_frame(column(GREY))
    w.process_frame(ok, 0.0)
    w.process_frame(ok, 0.5)
    noise = np.random.default_rng(3).integers(0, 256, size=ok.shape, dtype=np.uint8)
    for i in range(8):
        w.process_frame(noise, 1.0 + i * 0.5)
    assert ("auto:blind", BLIND_PRIORITY, "local:blind") in emitted
    assert sum(1 for e in emitted if e[2] == "local:blind") == 1
    assert "LOST" in w.status()


def test_replay_frames_reports_alert_frame():
    frames = [
        ("a.png", make_frame([])),
        ("b.png", make_frame([])),
        ("c.png", make_frame(column(RED))),
        ("d.png", make_frame(column(RED))),
    ]
    events = replay_frames(local_cfg(), frames)
    alerts = [e for e in events if e.kind == "alert"]
    assert len(alerts) == 1 and alerts[0].frame == "d.png"
