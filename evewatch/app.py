from __future__ import annotations

import logging
import logging.handlers
import shutil
import sys
import time
from pathlib import Path
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from .anchor import ViewAnchor, load_reference
from .capture import ScreenGrabber, enable_dpi_awareness
from .config import Config, ConfigError, load_config
from .intel_watch import ChatlogTailer, IntelWatcher, SystemTracker, resolve_chatlog_dir
from .local_watch import LocalWatcher, region_warning
from .metrics import MetricsLogger
from .sounds import FAULT_PRIORITY, NullBackend, SoundLibrary, SoundQueue, WinsoundBackend
from .universe import Universe, UniverseError

log = logging.getLogger("evewatch")

TICK_SEC = 0.1
STATUS_REFRESH_SEC = 1.0
FAULT_ERRORS = 5
FAULT_WINDOW_SEC = 60.0
CONFIG_POLL_SEC = 2.0


class Watcher(Protocol):
    name: str

    def tick(self, now: float) -> None: ...

    def status(self) -> str: ...


@dataclass
class RuntimeState:
    paused: bool = False


def configure_logging(config: Config) -> None:
    root = logging.getLogger("evewatch")
    root.setLevel(config.general.log_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            config.general.log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        print(f"warning: cannot open log file {config.general.log_file}: {exc}", file=sys.stderr)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("\n%(levelname)s: %(message)s"))
    root.addHandler(console)


def make_queue(config: Config, silent: bool = False) -> SoundQueue:
    library = SoundLibrary(config.sounds.directory, config.base_dir)
    backend = NullBackend() if silent else WinsoundBackend()
    return SoundQueue(backend, library, config.sounds.max_queue_age_sec)


def build_watchers(config: Config, queue: SoundQueue) -> list[Watcher]:
    watchers: list[Watcher] = []
    local_watcher: LocalWatcher | None = None
    local = config.local
    if local.enabled:
        if local.calibrated:
            grabber = ScreenGrabber()
            region = local.region
            warning = region_warning(region)
            if warning:
                log.warning(warning)
            anchor = None
            if local.anchor_region is not None:
                reference = load_reference(local.anchor_file)
                if reference is None:
                    log.warning(
                        "view anchor image %s missing; run `calibrate-anchor` (lost-view control is limited)",
                        local.anchor_file,
                    )
                elif reference.shape[:2] != (local.anchor_region[3], local.anchor_region[2]):
                    log.warning("view anchor image does not match anchor_region; run `calibrate-anchor` again")
                else:
                    anchor = ViewAnchor(local.anchor_region, reference, local.anchor_threshold)
            anchor_region = local.anchor_region
            local_watcher = LocalWatcher(
                local,
                lambda: grabber.grab(region),
                queue.submit,
                config.sounds.blind_sound,
                grabber.close,
                anchor=anchor,
                grab_anchor=(lambda: grabber.grab(anchor_region)) if anchor is not None else None,
            )
        else:
            log.warning("local.region is not calibrated; run `calibrate-region` or `settings`")

    intel = config.intel
    intel_active = intel.enabled and bool(intel.channels)
    tracker: SystemTracker | None = None
    chatlogs = resolve_chatlog_dir(intel.chatlog_dir)
    if local_watcher is not None or intel_active:
        if chatlogs.is_dir():
            tracker = SystemTracker(ChatlogTailer(chatlogs, "Local", from_start=True), intel.home_system)
        else:
            log.warning("chatlog folder not found: %s (system changes will not be detected)", chatlogs)
            tracker = SystemTracker(None, intel.home_system)
        if local_watcher is not None:
            tracker.subscribe(local_watcher.on_system_change)
        watchers.append(tracker)
    if local_watcher is not None:
        watchers.append(local_watcher)

    if intel.enabled and not intel.channels:
        log.warning("intel.channels is empty; the Intel watcher is off")
    if intel_active and tracker is not None:
        try:
            universe = Universe.load(intel.universe_file)
        except UniverseError as exc:
            log.warning("Intel watcher disabled: %s", exc)
        else:
            for a, b in universe.add_links(list(intel.extra_links)):
                log.warning("extra_links: unknown system in pair (%s, %s)", a, b)
            tailers = [ChatlogTailer(chatlogs, channel) for channel in intel.channels]
            watchers.append(IntelWatcher(intel, universe, tailers, tracker.home, queue.submit))
    return watchers


def _poll_keys() -> list[str]:
    try:
        import msvcrt
    except ImportError:
        return []
    keys = []
    while msvcrt.kbhit():
        keys.append(msvcrt.getwch().lower())
    return keys


def _status_line(state: RuntimeState, queue: SoundQueue, watchers: list[Watcher]) -> str:
    parts = ["PAUSED" if state.paused else "RUNNING", "sound: MUTED" if queue.muted else "sound: on"]
    parts.extend(w.status() for w in watchers or [])
    if not watchers:
        parts.append("no watchers active")
    return " | ".join(parts)


@dataclass
class Session:
    config: Config
    queue: SoundQueue
    watchers: list[Watcher]


def _start_session(config: Config, silent: bool = False) -> Session:
    queue = make_queue(config, silent)
    queue.prepare(
        [c.sound for c in config.local.classes.values() if c.enabled]
        + [config.sounds.blind_sound, config.sounds.fault_sound]
    )
    queue.start()
    return Session(config, queue, build_watchers(config, queue))


def _stop_session(session: Session) -> None:
    for watcher in session.watchers:
        close = getattr(watcher, "close", None)
        if close is not None:
            close()
    session.queue.close()


def _config_stamp(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def run(
    config: Config,
    config_path: Path | None = None,
    *,
    silent: bool = False,
    metrics_path: Path | None = None,
    metrics_interval: float = 60.0,
) -> int:
    configure_logging(config)
    enable_dpi_awareness()
    state = RuntimeState()
    session = _start_session(config, silent)
    metrics = None
    if metrics_path is not None:
        metrics = MetricsLogger(
            metrics_path,
            metrics_interval,
            extra=lambda: f"queue={session.queue.pending()} watchers={len(session.watchers)}",
        )
    errors: deque[float] = deque()
    last_fault = float("-inf")
    last_status = 0.0
    last_config_check = 0.0
    stamp = _config_stamp(config_path)
    reload_requested = False

    print("EVE Watch started. Keys: p = pause, m = mute, s = status, r = reload config, q = quit.")
    print("config.toml changes are picked up automatically.")
    log.info("started with %d watcher(s)", len(session.watchers))

    try:
        while True:
            now = time.monotonic()
            for key in _poll_keys():
                if key == "q":
                    return 0
                if key == "p":
                    state.paused = not state.paused
                elif key == "m":
                    session.queue.set_muted(not session.queue.muted)
                elif key == "r":
                    reload_requested = True
                elif key == "s":
                    print("\n" + _status_line(state, session.queue, session.watchers))
            if config_path is not None and now - last_config_check >= CONFIG_POLL_SEC:
                last_config_check = now
                current = _config_stamp(config_path)
                if current is not None and current != stamp:
                    stamp = current
                    reload_requested = True
            if reload_requested:
                reload_requested = False
                try:
                    new_config = load_config(config_path) if config_path is not None else None
                except ConfigError as exc:
                    log.warning("config not reloaded: %s", exc)
                    print(f"\nconfig not reloaded: {exc}")
                    new_config = None
                if new_config is not None:
                    muted = session.queue.muted
                    _stop_session(session)
                    session = _start_session(new_config, silent)
                    session.queue.set_muted(muted)
                    log.info("config reloaded; %d watcher(s)", len(session.watchers))
                    print("\nconfig reloaded")
            if not state.paused:
                for watcher in session.watchers:
                    try:
                        watcher.tick(now)
                    except Exception:
                        log.exception("watcher %s failed", watcher.name)
                        errors.append(now)
                        while errors and now - errors[0] > FAULT_WINDOW_SEC:
                            errors.popleft()
                        if len(errors) >= FAULT_ERRORS and now - last_fault > FAULT_WINDOW_SEC:
                            last_fault = now
                            session.queue.submit(session.config.sounds.fault_sound, FAULT_PRIORITY, "fault")
            if metrics is not None:
                metrics.tick(now)
            if now - last_status >= STATUS_REFRESH_SEC:
                last_status = now
                width = shutil.get_terminal_size((100, 20)).columns - 1
                line = _status_line(state, session.queue, session.watchers)[:width]
                sys.stdout.write("\r" + line.ljust(width))
                sys.stdout.flush()
            time.sleep(TICK_SEC)
    except KeyboardInterrupt:
        return 0
    finally:
        _stop_session(session)
        if metrics is not None:
            metrics.close()
        print("\nEVE Watch stopped.")
        log.info("stopped")
