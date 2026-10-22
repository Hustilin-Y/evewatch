from helpers import local_cfg
from evewatch.watchdog import ViewWatchdog

BG = (20, 20, 20)


def make(**kw):
    return ViewWatchdog(local_cfg(blind_after_sec=5, blind_repeat_sec=20, **kw))


def test_never_blind_with_healthy_frames():
    w = make(background_color=BG)
    for i in range(50):
        assert not w.update(3, 0.02, i)
    assert not w.blind


def test_blind_after_delay_then_repeats():
    w = make(background_color=BG)
    fired = [t for t in range(0, 60) if w.update(0, 0.9, float(t))]
    assert fired == [5, 25, 45]
    assert w.blind


def test_recovery_clears_blind():
    w = make(background_color=BG)
    for t in range(8):
        w.update(0, 0.9, float(t))
    assert w.blind
    assert not w.update(2, 0.01, 9.0)
    assert not w.blind


def test_short_glitch_is_ignored():
    w = make(background_color=BG)
    assert not w.update(2, 0.9, 0.0)
    assert not w.update(2, 0.9, 3.0)
    assert not w.update(2, 0.0, 4.0)
    assert not w.update(2, 0.9, 6.0)
    assert not w.blind


def test_empty_after_icons_means_blind():
    w = make(blind_on_empty=True)
    w.update(2, None, 0.0)
    assert not w.update(0, None, 1.0)
    assert w.update(0, None, 6.0)


def test_empty_without_icons_ever_is_not_blind():
    w = make(blind_on_empty=True)
    assert not any(w.update(0, None, float(t)) for t in range(30))


def test_blind_on_empty_is_off_by_default():
    w = make()
    w.update(2, None, 0.0)
    assert not any(w.update(0, None, float(t)) for t in range(1, 30))


def test_reset_forgets_seen_icons():
    w = make(blind_on_empty=True)
    w.update(2, None, 0.0)
    w.reset()
    assert not any(w.update(0, None, float(t)) for t in range(1, 30))


def test_second_loss_is_signalled_on_time():
    w = make(background_color=BG)
    fired = []
    schedule = [(t, 0.9) for t in range(0, 10)] + [(t, 0.0) for t in range(10, 20)] + [(t, 0.9) for t in range(20, 30)]
    for t, foreign in schedule:
        if w.update(0, foreign, float(t)):
            fired.append(t)
    assert fired == [5, 25]
