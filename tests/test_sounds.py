import threading
import wave
from pathlib import Path

import numpy as np
import pytest

from evewatch.sounds import (
    SAMPLE_RATE,
    SoundLibrary,
    SoundQueue,
    parse_auto,
    synthesize,
    write_wav,
)


class FakeBackend:
    def __init__(self, blocking=False):
        self.played = []
        self.stops = 0
        self.blocking = blocking
        self.started = threading.Event()
        self._release = threading.Event()
        self._interrupted = False

    def play(self, sound):
        self.played.append(sound.spec)
        self.started.set()
        if not self.blocking:
            return True
        self._release.wait(2)
        self._release.clear()
        interrupted, self._interrupted = self._interrupted, False
        return not interrupted

    def finish(self):
        self._release.set()

    def stop(self):
        self.stops += 1
        self._interrupted = True
        self._release.set()


def make_queue(tmp_path, backend=None, max_age=5.0, clock=None):
    library = SoundLibrary(tmp_path / "cache", tmp_path)
    kwargs = {"clock": clock} if clock else {}
    return SoundQueue(backend or FakeBackend(), library, max_age, **kwargs)


def wait_until(predicate, timeout=2.0):
    done = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return True
        done.wait(0.01)
    return predicate()


def test_parse_auto_known_and_unknown():
    for name in ("terrible", "bad", "neutral", "good", "excellent", "blind", "fault", "intel_0", "intel_12", "tone_1000_2"):
        assert parse_auto(name) is not None, name
    for name in ("", "nope", "intel_", "intel_100", "tone_50_2", "tone_1000_9", "tone_1000_0"):
        assert parse_auto(name) is None, name


def test_danger_ordering():
    freqs = [parse_auto(n).freqs[0] for n in ("excellent", "good", "neutral", "bad", "terrible")]
    assert freqs == sorted(freqs)
    intel = [parse_auto(f"intel_{n}") for n in (0, 2, 5, 8)]
    assert [s.freqs[0] for s in intel] == sorted((s.freqs[0] for s in intel), reverse=True)
    assert [s.pulses for s in intel] == sorted((s.pulses for s in intel), reverse=True)


def test_all_default_sounds_are_distinct():
    names = ["terrible", "bad", "neutral", "good", "excellent", "blind", "fault", "intel_0", "intel_2", "intel_5"]
    waves = {n: tuple(synthesize(parse_auto(n))) for n in names}
    assert len(set(waves.values())) == len(names)


def _harmonic_ratio(name):
    spec = parse_auto(name)
    samples = np.array(synthesize(spec)[: int(SAMPLE_RATE * spec.pulse_sec)], dtype=float)
    mag = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    freqs = np.fft.rfftfreq(len(samples), 1 / SAMPLE_RATE)

    def peak(f):
        band = (freqs > f - 40) & (freqs < f + 40)
        return mag[band].max()

    f0 = spec.freqs[0]
    return peak(3 * f0) / peak(f0)


def test_local_and_intel_timbres_differ():
    local = [_harmonic_ratio(n) for n in ("neutral", "bad", "good")]
    intel = [_harmonic_ratio(n) for n in ("intel_0", "intel_2", "intel_5")]
    assert min(local) > 0.2
    assert max(intel) < 0.05


def test_wav_file_is_valid(tmp_path):
    spec = parse_auto("terrible")
    path = tmp_path / "t.wav"
    write_wav(path, synthesize(spec))
    with wave.open(str(path), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, SAMPLE_RATE)
        assert w.getnframes() / SAMPLE_RATE == pytest.approx(spec.duration_sec, abs=0.01)


def test_library_generates_and_caches(tmp_path):
    lib = SoundLibrary(tmp_path / "cache", tmp_path)
    sound = lib.resolve("auto:intel_2")
    assert sound.path.is_file() and sound.path.parent == tmp_path / "cache"
    assert lib.resolve("auto:intel_2") is sound
    assert sound.fallback_pulses == 3


def test_library_custom_wav_and_missing_file(tmp_path):
    write_wav(tmp_path / "mine.wav", synthesize(parse_auto("good")))
    lib = SoundLibrary(tmp_path / "cache", tmp_path)
    mine = lib.resolve("mine.wav")
    assert mine.path == tmp_path / "mine.wav" and mine.duration_sec > 0
    missing = lib.resolve("gone.wav")
    assert missing.path == tmp_path / "gone.wav" and not missing.path.exists()


def test_queue_plays_by_priority(tmp_path):
    backend = FakeBackend()
    q = make_queue(tmp_path, backend)
    q.submit("auto:good", 1)
    q.submit("auto:terrible", 5)
    q.submit("auto:bad", 4)
    q.start()
    assert wait_until(lambda: len(backend.played) == 3)
    q.close()
    assert backend.played == ["auto:terrible", "auto:bad", "auto:good"]


def test_queue_drops_stale_items(tmp_path):
    now = [0.0]
    q = make_queue(tmp_path, max_age=5.0, clock=lambda: now[0])
    q.submit("auto:good", 1)
    now[0] = 3.0
    q.submit("auto:bad", 4)
    now[0] = 6.0
    with q._cond:
        item = q._pop_locked()
        assert item.spec == "auto:bad"
        assert q._pop_locked() is None


def test_higher_priority_interrupts_and_low_is_replayed(tmp_path):
    backend = FakeBackend(blocking=True)
    q = make_queue(tmp_path, backend)
    q.start()
    q.submit("auto:good", 1)
    assert backend.started.wait(2)
    q.submit("auto:terrible", 5)
    assert wait_until(lambda: backend.played == ["auto:good", "auto:terrible", "auto:good"])
    assert backend.stops == 1
    q.close()


def test_equal_priority_does_not_interrupt(tmp_path):
    backend = FakeBackend(blocking=True)
    q = make_queue(tmp_path, backend)
    q.start()
    q.submit("auto:bad", 4)
    assert backend.started.wait(2)
    q.submit("auto:bad", 4)
    backend.finish()
    assert wait_until(lambda: len(backend.played) == 2)
    assert backend.played == ["auto:bad", "auto:bad"]
    assert backend.stops == 0
    backend.finish()
    q.close()


def test_mute_drops_pending_and_rejects_new(tmp_path):
    q = make_queue(tmp_path)
    assert q.submit("auto:good", 1)
    q.set_muted(True)
    assert not q.submit("auto:bad", 4)
    with q._cond:
        assert q._pop_locked() is None
    q.set_muted(False)
    assert q.submit("auto:bad", 4)


def test_backend_error_does_not_kill_worker(tmp_path):
    class Exploding(FakeBackend):
        def play(self, sound):
            super().play(sound)
            if len(self.played) == 1:
                raise RuntimeError("boom")

    backend = Exploding()
    q = make_queue(tmp_path, backend)
    q.start()
    q.submit("auto:bad", 4)
    q.submit("auto:good", 1)
    assert wait_until(lambda: len(backend.played) == 2)
    q.close()


class FakeWinsound:
    SND_FILENAME, SND_ASYNC, SND_NODEFAULT, SND_PURGE = 1, 2, 4, 8

    def __init__(self, fail_play=False):
        self.calls = []
        self.fail_play = fail_play

    def PlaySound(self, target, flags):
        self.calls.append(("play", target, flags))
        if self.fail_play and target is not None:
            raise RuntimeError("Failed to play sound")

    def Beep(self, freq, ms):
        self.calls.append(("beep", freq, ms))


def resolved(path, **kw):
    from evewatch.sounds import ResolvedSound

    return ResolvedSound("x", path, kw.pop("duration", 0.05), kw.pop("freq", 700), kw.pop("pulses", 3))


def test_backend_stop_when_idle_is_a_noop(tmp_path):
    from evewatch.sounds import WinsoundBackend

    ws = FakeWinsound()
    backend = WinsoundBackend(ws)
    backend.stop()  # late preemption: nothing is playing
    wav = tmp_path / "a.wav"
    write_wav(wav, synthesize(parse_auto("good")))
    assert backend.play(resolved(wav)) is True
    kinds = [c[0] for c in ws.calls]
    assert kinds == ["play"]  # no purge, and the sound really played
    assert ws.calls[0][2] & ws.SND_NODEFAULT


def test_backend_stop_interrupts_active_play(tmp_path):
    from evewatch.sounds import WinsoundBackend

    ws = FakeWinsound()
    backend = WinsoundBackend(ws)
    wav = tmp_path / "a.wav"
    write_wav(wav, synthesize(parse_auto("good")))
    result = []
    t = threading.Thread(target=lambda: result.append(backend.play(resolved(wav, duration=2.0))))
    t.start()
    assert wait_until(lambda: any(c[0] == "play" for c in ws.calls))
    backend.stop()
    t.join(2)
    assert result == [False]
    assert ("play", None, ws.SND_PURGE) in ws.calls


def test_backend_beep_fallback_has_gaps_and_correct_count(tmp_path, monkeypatch):
    from evewatch.sounds import WinsoundBackend

    ws = FakeWinsound()
    backend = WinsoundBackend(ws)
    waits = []
    real_wait = backend._stop.wait
    monkeypatch.setattr(backend._stop, "wait", lambda t=None: waits.append(t) or False)
    assert backend.play(resolved(tmp_path / "missing.wav", freq=900, pulses=3)) is True
    assert [c for c in ws.calls if c[0] == "beep"] == [("beep", 900, 90)] * 3
    assert waits == [WinsoundBackend.BEEP_GAP_SEC] * 2
    assert real_wait is not None


def test_backend_falls_back_to_beep_when_playsound_raises(tmp_path):
    from evewatch.sounds import WinsoundBackend

    ws = FakeWinsound(fail_play=True)
    wav = tmp_path / "bad.wav"
    wav.write_bytes(b"not a wav")
    backend = WinsoundBackend(ws)
    assert backend.play(resolved(wav, freq=800, pulses=2)) is True
    assert [c[0] for c in ws.calls] == ["play", "beep", "beep"]


def test_custom_wav_fallback_scales_with_priority(tmp_path):
    seen = []

    class Recorder(FakeBackend):
        def play(self, sound):
            seen.append((sound.fallback_freq, sound.fallback_pulses))
            return True

    q = make_queue(tmp_path, Recorder())
    q.start()
    q.submit("mine.wav", 5)
    q.submit("other.wav", 1)
    assert wait_until(lambda: len(seen) == 2)
    q.close()
    assert seen[0] == (1100, 4) and seen[1] == (620, 1)
