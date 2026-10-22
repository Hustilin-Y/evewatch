from __future__ import annotations

import dataclasses
import heapq
import itertools
import logging
import math
import re
import struct
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger(__name__)

SAMPLE_RATE = 22050
SYNTH_VERSION = 1
BLIND_PRIORITY = 100
FAULT_PRIORITY = 90


@dataclass(frozen=True)
class AutoSpec:
    freqs: tuple[float, ...]
    pulses: int
    timbre: str
    pulse_sec: float = 0.09
    gap_sec: float = 0.06

    @property
    def duration_sec(self) -> float:
        return self.pulses * self.pulse_sec + (self.pulses - 1) * self.gap_sec


_LOCAL_TONES = {
    "terrible": (1320.0, 4),
    "bad": (1100.0, 3),
    "neutral": (880.0, 2),
    "good": (660.0, 1),
    "excellent": (520.0, 1),
}
_INTEL_RE = re.compile(r"intel_(\d{1,2})")
_TONE_RE = re.compile(r"tone_(\d{3,4})_(\d)")


def parse_auto(name: str) -> AutoSpec | None:
    if name in _LOCAL_TONES:
        freq, pulses = _LOCAL_TONES[name]
        return AutoSpec((freq,), pulses, "local")
    if name == "blind":
        return AutoSpec((1000.0, 650.0), 6, "local", pulse_sec=0.12, gap_sec=0.04)
    if name == "clear":
        return AutoSpec((700.0,), 1, "intel", pulse_sec=0.18)
    if name == "fault":
        return AutoSpec((220.0,), 2, "local", pulse_sec=0.3, gap_sec=0.1)
    if m := _INTEL_RE.fullmatch(name):
        jumps = int(m.group(1))
        pulses = 4 if jumps == 0 else 3 if jumps <= 2 else 2 if jumps <= 5 else 1
        return AutoSpec((max(500.0, 1400.0 - 100.0 * jumps),), pulses, "intel")
    if m := _TONE_RE.fullmatch(name):
        freq, pulses = int(m.group(1)), int(m.group(2))
        if 100 <= freq <= 4000 and 1 <= pulses <= 8:
            return AutoSpec((float(freq),), pulses, "local")
    return None


def is_valid_auto_name(name: str) -> bool:
    return parse_auto(name) is not None


def _oscillator(timbre: str, freq: float, t: float, progress: float) -> float:
    w = 2 * math.pi * freq * t
    if timbre == "intel":
        return 0.6 * math.sin(w) * math.exp(-1.5 * progress)
    return 0.4 * (math.sin(w) + math.sin(3 * w) / 3 + math.sin(5 * w) / 5 + math.sin(7 * w) / 7)


def synthesize(spec: AutoSpec) -> list[int]:
    out: list[int] = []
    fade = int(SAMPLE_RATE * 0.005)
    n = int(SAMPLE_RATE * spec.pulse_sec)
    gap = [0] * int(SAMPLE_RATE * spec.gap_sec)
    for i in range(spec.pulses):
        freq = spec.freqs[i % len(spec.freqs)]
        for k in range(n):
            env = min(1.0, k / fade, (n - 1 - k) / fade)
            v = _oscillator(spec.timbre, freq, k / SAMPLE_RATE, k / n) * env
            out.append(int(max(-1.0, min(1.0, v)) * 32767))
        if i < spec.pulses - 1:
            out.extend(gap)
    return out


def write_wav(path: Path, samples: list[int]) -> None:
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    tmp.replace(path)


@dataclass(frozen=True)
class ResolvedSound:
    spec: str
    path: Path | None
    duration_sec: float
    fallback_freq: int
    fallback_pulses: int


class SoundLibrary:
    def __init__(self, cache_dir: Path, base_dir: Path):
        self._cache_dir = cache_dir
        self._base_dir = base_dir
        self._resolved: dict[str, ResolvedSound] = {}
        self._lock = threading.Lock()

    def resolve(self, spec: str) -> ResolvedSound:
        with self._lock:
            sound = self._resolved.get(spec)
            if sound is None:
                sound = self._build(spec)
                self._resolved[spec] = sound
            return sound

    def _build(self, spec: str) -> ResolvedSound:
        if spec.startswith("auto:"):
            name = spec[5:]
            auto = parse_auto(name)
            if auto is not None:
                path: Path | None = self._cache_dir / f"{name}.v{SYNTH_VERSION}.wav"
                try:
                    if not path.is_file():
                        self._cache_dir.mkdir(parents=True, exist_ok=True)
                        write_wav(path, synthesize(auto))
                except OSError:
                    log.warning("cannot write generated sound %s, using beep fallback", name)
                    path = None
                return ResolvedSound(spec, path, auto.duration_sec, int(auto.freqs[0]), auto.pulses)
            log.warning("unknown auto sound %r, using beep fallback", spec)
            return ResolvedSound(spec, None, 0.3, 800, 1)
        path = Path(spec)
        if not path.is_absolute():
            path = self._base_dir / path
        duration = 1.0
        try:
            with wave.open(str(path), "rb") as wav:
                duration = wav.getnframes() / wav.getframerate()
        except (OSError, EOFError, wave.Error):
            log.warning("cannot read sound file %s, using beep fallback", path)
        return ResolvedSound(spec, path, duration, 800, 1)


class AudioBackend(Protocol):
    def play(self, sound: ResolvedSound) -> bool:
        """Blocks until the sound ends. Returns False if it was cut short by stop()."""
        ...

    def stop(self) -> None: ...


class NullBackend:
    """Plays nothing; used for measurements (`run --silent`)."""

    def play(self, sound: ResolvedSound) -> bool:
        return True

    def stop(self) -> None:
        pass


class WinsoundBackend:
    BEEP_MS = 90
    BEEP_GAP_SEC = 0.06

    def __init__(self, module=None) -> None:
        if module is None:
            import winsound as module
        self._ws = module
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._active = False

    def play(self, sound: ResolvedSound) -> bool:
        ws = self._ws
        with self._lock:
            self._stop.clear()
            self._active = True
        try:
            if sound.path is not None and sound.path.is_file():
                try:
                    ws.PlaySound(str(sound.path), ws.SND_FILENAME | ws.SND_ASYNC | ws.SND_NODEFAULT)
                except RuntimeError:
                    log.warning("PlaySound failed for %s, using beep fallback", sound.path)
                else:
                    return not self._stop.wait(sound.duration_sec + 0.05)
            return self._beep(sound)
        finally:
            with self._lock:
                self._active = False

    def _beep(self, sound: ResolvedSound) -> bool:
        for i in range(sound.fallback_pulses):
            if i and self._stop.wait(self.BEEP_GAP_SEC):
                return False
            if self._stop.is_set():
                return False
            try:
                self._ws.Beep(sound.fallback_freq, self.BEEP_MS)
            except RuntimeError:
                break
        return not self._stop.is_set()

    def stop(self) -> None:
        with self._lock:
            if not self._active:
                return
            self._stop.set()
            try:
                self._ws.PlaySound(None, self._ws.SND_PURGE)
            except RuntimeError:
                pass


@dataclass(order=True)
class _Item:
    neg_priority: int
    seq: int
    enqueued_at: float = field(compare=False)
    spec: str = field(compare=False)
    priority: int = field(compare=False)
    label: str = field(compare=False)


class SoundQueue:
    def __init__(
        self,
        backend: AudioBackend,
        library: SoundLibrary,
        max_age_sec: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._backend = backend
        self._library = library
        self._max_age = max_age_sec
        self._clock = clock
        self._heap: list[_Item] = []
        self._seq = itertools.count()
        self._cond = threading.Condition()
        self._current: _Item | None = None
        self._preempted = False
        self._muted = False
        self._closing = False
        self._thread: threading.Thread | None = None

    @property
    def muted(self) -> bool:
        return self._muted

    def pending(self) -> int:
        with self._cond:
            return len(self._heap)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="sound-queue", daemon=True)
        self._thread.start()

    def close(self) -> None:
        with self._cond:
            self._closing = True
            self._heap.clear()
            if self._current is not None:
                self._backend.stop()
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def prepare(self, specs: list[str]) -> None:
        for spec in specs:
            self._library.resolve(spec)

    def set_muted(self, muted: bool) -> None:
        with self._cond:
            self._muted = muted
            if muted:
                self._heap.clear()
                if self._current is not None:
                    self._backend.stop()

    def submit(self, spec: str, priority: int, label: str = "") -> bool:
        with self._cond:
            if self._muted or self._closing:
                return False
            heapq.heappush(
                self._heap, _Item(-priority, next(self._seq), self._clock(), spec, priority, label)
            )
            if self._current is not None and priority > self._current.priority and not self._preempted:
                self._preempted = True
                self._backend.stop()
            self._cond.notify()
        return True

    def _pop_locked(self) -> _Item | None:
        now = self._clock()
        while self._heap:
            item = heapq.heappop(self._heap)
            if now - item.enqueued_at > self._max_age:
                log.debug("dropped stale sound %s (%s)", item.spec, item.label)
                continue
            return item
        return None

    def _run(self) -> None:
        while True:
            with self._cond:
                item = None
                while not self._closing:
                    item = self._pop_locked()
                    if item is not None:
                        break
                    self._cond.wait()
                if self._closing:
                    return
                self._current = item
                self._preempted = False
            completed = True
            try:
                sound = self._library.resolve(item.spec)
                if not item.spec.startswith("auto:"):
                    sound = dataclasses.replace(
                        sound,
                        fallback_freq=min(2000, 500 + 120 * item.priority),
                        fallback_pulses=max(1, min(4, item.priority)),
                    )
                completed = self._backend.play(sound) is not False
            except Exception:
                log.exception("sound playback failed: %s", item.spec)
            with self._cond:
                if not completed and not self._muted and not self._closing:
                    heapq.heappush(self._heap, dataclasses.replace(item, seq=next(self._seq)))
                self._current = None
                self._preempted = False
