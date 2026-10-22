from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .anchor import ViewAnchor
from .config import ClassConfig, LocalConfig
from .sounds import BLIND_PRIORITY
from .watchdog import ViewWatchdog

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FrameResult:
    counts: dict[str, int]
    foreign_fraction: float | None


@dataclass(frozen=True)
class Alert:
    class_name: str
    sound: str
    priority: int
    count: int
    reason: str


ICON_COLUMN_MAX_WIDTH = 80


def region_warning(region: tuple[int, int, int, int]) -> str | None:
    if region[2] > ICON_COLUMN_MAX_WIDTH:
        return (
            f"local.region is {region[2]} px wide: it should be only the narrow column of standing icons "
            f"(about 12-20 px), otherwise chat text and member names are counted as icons"
        )
    return None


def count_runs(rows: np.ndarray, min_height: int, merge_gap: int) -> int:
    idx = np.flatnonzero(rows)
    if idx.size == 0:
        return 0
    breaks = np.flatnonzero(np.diff(idx) > merge_gap + 1)
    starts = np.concatenate(([idx[0]], idx[breaks + 1]))
    ends = np.concatenate((idx[breaks], [idx[-1]]))
    return int(np.count_nonzero(ends - starts + 1 >= min_height))


def classify_frame(frame: np.ndarray, cfg: LocalConfig) -> FrameResult:
    classes = list(cfg.classes.values())
    height, width, _ = frame.shape
    packed = (
        (frame[:, :, 0].astype(np.int32) << 16) | (frame[:, :, 1].astype(np.int32) << 8) | frame[:, :, 2]
    ).ravel()
    unique, inverse = np.unique(packed, return_inverse=True)
    colors = np.stack(((unique >> 16) & 255, (unique >> 8) & 255, unique & 255), axis=1).astype(np.int16)

    class_colors = np.array([c.color for c in classes], dtype=np.int16)
    tolerances = np.array([c.tolerance for c in classes], dtype=np.int16)
    diff = np.abs(colors[:, None, :] - class_colors[None, :, :]).max(axis=2)
    within = diff <= tolerances
    best_u = np.where(within, diff, 256).argmin(axis=1)
    matched_u = within.any(axis=1)
    best = best_u[inverse].reshape(height, width)
    matched = matched_u[inverse].reshape(height, width)

    counts: dict[str, int] = {}
    for k, cls in enumerate(classes):
        rows = (matched & (best == k)).sum(axis=1) >= cfg.min_row_pixels
        counts[cls.name] = count_runs(rows, cfg.min_icon_height, cfg.merge_gap_rows)

    foreign = None
    if cfg.background_color is not None:
        bg_u = np.abs(colors - np.array(cfg.background_color, dtype=np.int16)).max(axis=1) <= cfg.background_tolerance
        known = matched | bg_u[inverse].reshape(height, width)
        foreign = 1.0 - float(known.mean())
    return FrameResult(counts, foreign)


class LocalStateMachine:
    """Turns stable per-class icon counts into at most one alert per update.

    `_announced` holds, per class, the count that has already been dealt with:
    growth above it is pending. Pending growth of a class in cooldown is kept
    and fires when the cooldown ends; growth that lost the priority contest to
    another class is consumed (only the most dangerous class plays).
    """

    def __init__(self, cfg: LocalConfig):
        self._cfg = cfg
        self.stable: dict[str, int] | None = None
        self._candidate: dict[str, int] | None = None
        self._streak = 0
        self._arrival = False
        self._announced: dict[str, int] = {}
        self._last_signal: dict[str, float] = {}
        self._repeat_ref: dict[str, float] = {}

    def reset(self, arrival: bool = True) -> None:
        self.stable = None
        self._candidate = None
        self._streak = 0
        self._arrival = arrival and self._cfg.alert_on_arrival
        self._announced = {}
        self._last_signal.clear()
        self._repeat_ref.clear()

    def update(self, counts: dict[str, int], now: float) -> list[Alert]:
        if counts == self._candidate:
            self._streak += 1
        else:
            self._candidate = dict(counts)
            self._streak = 1
        if self._streak < self._cfg.confirm_frames:
            return []

        classes = self._cfg.classes
        if self.stable is None:
            self._announced = {} if self._arrival else dict(counts)
            if not self._arrival:
                for name, cls in classes.items():
                    if cls.enabled and counts.get(name, 0) > 0:
                        self._repeat_ref[name] = now
        self.stable = dict(counts)

        eligible: list[Alert] = []
        for name, cls in classes.items():
            n = counts.get(name, 0)
            if n < self._announced.get(name, 0):
                self._announced[name] = n
            if n == 0:
                self._repeat_ref.pop(name, None)
            if n > self._announced.get(name, 0):
                if not cls.enabled:
                    self._announced[name] = n
                elif self._cooled_down(name, now):
                    eligible.append(
                        Alert(name, cls.sound, cls.priority, n, "arrival" if self._arrival else "grew")
                    )
        self._arrival = False
        for alert in eligible:
            self._announced[alert.class_name] = alert.count

        candidates = list(eligible)
        if self._cfg.repeat_while_present_sec > 0:
            fired = {a.class_name for a in eligible}
            for name, cls in classes.items():
                n = counts.get(name, 0)
                if cls.enabled and n > 0 and name not in fired:
                    ref = self._repeat_ref.setdefault(name, now)
                    if now - ref >= self._cfg.repeat_while_present_sec:
                        candidates.append(Alert(name, cls.sound, cls.priority, n, "repeat"))
        if not candidates:
            return []
        best = max(candidates, key=lambda a: a.priority)
        self._last_signal[best.class_name] = now
        self._repeat_ref[best.class_name] = now
        return [best]

    def _cooled_down(self, name: str, now: float) -> bool:
        return now - self._last_signal.get(name, float("-inf")) >= self._cfg.cooldown_sec


Emit = Callable[[str, int, str], object]


class LocalWatcher:
    name = "local"

    def __init__(
        self,
        cfg: LocalConfig,
        grab: Callable[[], np.ndarray],
        emit: Emit,
        blind_sound: str,
        on_close: Callable[[], None] | None = None,
        anchor: ViewAnchor | None = None,
        grab_anchor: Callable[[], np.ndarray] | None = None,
    ):
        self._cfg = cfg
        self._grab = grab
        self._emit = emit
        self._blind_sound = blind_sound
        self._machine = LocalStateMachine(cfg)
        self._machine.reset(arrival=False)
        self._watchdog = ViewWatchdog(cfg)
        self._interval = 1.0 / cfg.fps
        self._next_at = 0.0
        self._counts: dict[str, int] = {}
        self.system: str | None = None
        self._on_close = on_close
        self._anchor = anchor
        self._grab_anchor = grab_anchor
        self.anchor_score: float | None = None

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def close(self) -> None:
        if self._on_close is not None:
            self._on_close()

    def on_system_change(self, system: str | None) -> None:
        log.info("system changed to %s; resetting Local baseline", system)
        self.system = system
        self._machine.reset(arrival=True)
        self._watchdog.reset()

    def tick(self, now: float) -> None:
        if now < self._next_at:
            return
        self._next_at = now + self._interval
        anchor_patch = self._grab_anchor() if self._anchor is not None and self._grab_anchor is not None else None
        self.process_frame(self._grab(), now, anchor_patch)

    def process_frame(self, frame: np.ndarray, now: float, anchor_patch: np.ndarray | None = None) -> list[Alert]:
        result = classify_frame(frame, self._cfg)
        self._counts = result.counts
        total = sum(result.counts.values())
        anchor_ok = None
        if self._anchor is not None and anchor_patch is not None:
            self.anchor_score = self._anchor.score(anchor_patch)
            anchor_ok = self.anchor_score >= self._anchor.threshold
        if self._watchdog.update(total, result.foreign_fraction, now, anchor_ok):
            self._emit(self._blind_sound, BLIND_PRIORITY, "local:blind")
        if self._watchdog.blind:
            return []
        alerts = self._machine.update(result.counts, now)
        for a in alerts:
            log.info("alert %s x%d (%s)", a.class_name, a.count, a.reason)
            self._emit(a.sound, a.priority, f"local:{a.class_name}")
        return alerts

    def status(self) -> str:
        shown = " ".join(f"{n[:3]}:{c}" for n, c in self._counts.items() if self._cfg.classes[n].enabled)
        view = "view LOST" if self._watchdog.blind else "view ok"
        return f"local {shown or '-'} {view}"


@dataclass(frozen=True)
class ReplayEvent:
    frame: str
    time: float
    kind: str
    detail: str


def replay_frames(
    cfg: LocalConfig, frames: Iterable[tuple[str, np.ndarray]], blind_sound: str = "auto:blind"
) -> list[ReplayEvent]:
    """Runs frames through the watcher with a simulated clock (1/fps per frame)."""
    events: list[ReplayEvent] = []
    current = {"frame": "", "time": 0.0}

    def emit(sound: str, priority: int, label: str) -> None:
        events.append(ReplayEvent(current["frame"], current["time"], "alert", f"{label} {sound}"))

    watcher = LocalWatcher(cfg, lambda: np.zeros((1, 1, 3), np.uint8), emit, blind_sound)
    step = 1.0 / cfg.fps
    for i, (name, frame) in enumerate(frames):
        current["frame"], current["time"] = name, i * step
        watcher.process_frame(frame, i * step)
        counts = " ".join(f"{n}={c}" for n, c in watcher.counts.items())
        events.append(ReplayEvent(name, i * step, "frame", counts))
    return events


def load_frames(folder: Path, crop: tuple[int, int, int, int] | None = None) -> list[tuple[str, np.ndarray]]:
    from PIL import Image

    paths = sorted(p for p in folder.iterdir() if p.suffix.lower() in {".png", ".bmp", ".jpg", ".jpeg"})
    frames = []
    for path in paths:
        with Image.open(path) as img:
            arr = np.asarray(img.convert("RGB"))
        if crop is not None:
            x, y, w, h = crop
            arr = arr[y : y + h, x : x + w]
        frames.append((path.name, arr))
    return frames
