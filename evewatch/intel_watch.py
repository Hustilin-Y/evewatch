from __future__ import annotations

import ctypes
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import IntelConfig, TierConfig
from .universe import Universe

log = logging.getLogger(__name__)

LINE_RE = re.compile(r"^﻿?\[ (\d{4})\.(\d\d)\.(\d\d) (\d\d):(\d\d):(\d\d) \] (.*?) > (.*)$")
SYSTEM_CHANGE_RE = re.compile(r"^Channel changed to Local : (.+?)\s*$")
FILE_RE = re.compile(r"^(?P<channel>.+)_(?P<date>\d{8})_(?P<time>\d{6})_(?P<char>\d+)\.txt$")
TOKEN_RE = re.compile(r"[\w'][\w'\-]*")

CLEAR_PRIORITY = 1
REPORT_PRIORITY_BASE = 10
SYSTEM_SPEAKER = "EVE System"


def documents_dir() -> Path:
    """The user's Documents folder (follows OneDrive redirection), via SHGetKnownFolderPath."""
    try:

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        raw = uuid.UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7").bytes_le
        guid = GUID.from_buffer_copy(raw)
        path_ptr = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(GUID),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr)) == 0 and path_ptr.value:
            result = Path(path_ptr.value)
            ctypes.windll.ole32.CoTaskMemFree(path_ptr)
            return result
    except (AttributeError, OSError):
        pass
    return Path.home() / "Documents"


def resolve_chatlog_dir(setting: str) -> Path:
    if setting.strip().lower() == "auto":
        return documents_dir() / "EVE" / "logs" / "Chatlogs"
    return Path(setting)


class ChatlogTailer:
    """Follows the newest chatlog file of one channel and yields complete new lines.

    Files are UTF-16 LE with a BOM; message lines are CRLF-terminated and begin with U+FEFF.
    History is not replayed on attach unless from_start is set (used for Local to learn
    the current system). A newer file appearing later (new session, new day) is read from its start.
    """

    def __init__(
        self,
        directory: Path,
        channel: str,
        *,
        from_start: bool = False,
        rescan_sec: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.directory = directory
        self.channel = channel
        self.path: Path | None = None
        self._from_start = from_start
        self._rescan_sec = rescan_sec
        self._clock = clock
        self._last_scan = float("-inf")
        self._dir_mtime: int | None = None
        self._dir_checked_wall = 0.0
        self._offset = 0
        self._byte_rest = b""
        self._text_rest = ""

    def _newest(self) -> Path | None:
        best: tuple[str, float, Path] | None = None
        wanted = self.channel.lower()
        prefix = wanted + "_"
        try:
            entries = list(os.scandir(self.directory))
        except OSError:
            return None
        for entry in entries:
            if not entry.name.lower().startswith(prefix):
                continue
            m = FILE_RE.match(entry.name)
            if not m or m.group("channel").lower() != wanted:
                continue
            key = (m.group("date") + m.group("time"), entry.stat().st_mtime)
            if best is None or key > best[:2]:
                best = (*key, Path(entry.path))
        return best[2] if best else None

    def _directory_changed(self) -> bool:
        """False only when the folder was last modified more than a second before the previous check
        and has not been modified since, so the (large) listing need not be read again."""
        try:
            mtime = os.stat(self.directory).st_mtime_ns
        except OSError:
            return True
        unchanged = (
            self.path is not None
            and mtime == self._dir_mtime
            and mtime / 1e9 < self._dir_checked_wall - 1.0
        )
        self._dir_mtime = mtime
        self._dir_checked_wall = time.time()
        return not unchanged

    def _attach(self, path: Path) -> None:
        initial = self.path is None
        self.path = path
        self._byte_rest = b""
        self._text_rest = ""
        if initial and not self._from_start:
            try:
                self._offset = path.stat().st_size & ~1
            except OSError:
                self._offset = 0
        else:
            self._offset = 0
        log.info("following %s (offset %d)", path.name, self._offset)

    def poll(self) -> list[str]:
        now = self._clock()
        if self.path is None or now - self._last_scan >= self._rescan_sec:
            self._last_scan = now
            newest = self._newest() if self._directory_changed() else None
            if newest is not None and newest != self.path:
                self._attach(newest)
        if self.path is None:
            return []
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self._offset:
            log.info("%s was truncated; reading from the start", self.path.name)
            self._offset, self._byte_rest, self._text_rest = 0, b"", ""
        if size == self._offset:
            return []
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read(min(size - self._offset, 4 * 1024 * 1024))
        except OSError:
            return []
        at_start = self._offset == 0
        self._offset += len(chunk)
        data = self._byte_rest + chunk
        if at_start and data.startswith(b"\xff\xfe"):
            data = data[2:]
        usable = len(data) & ~1
        if usable >= 2 and 0xD8 <= data[usable - 1] <= 0xDB:  # high surrogate: wait for its pair
            usable -= 2
        self._byte_rest = data[usable:]
        self._text_rest += data[:usable].decode("utf-16-le", errors="replace")
        *lines, self._text_rest = self._text_rest.split("\n")
        return [line.rstrip("\r") for line in lines if line.strip()]


class SystemTracker:
    """Current system from the Local chatlog; notifies subscribers on every system change."""

    name = "system"

    def __init__(self, tailer: ChatlogTailer | None, home_system: str = "auto"):
        self._tailer = tailer
        self._fixed = None if home_system.strip().lower() == "auto" else home_system.strip()
        self._current: str | None = None
        self._listeners: list[Callable[[str], None]] = []

    def subscribe(self, callback: Callable[[str], None]) -> None:
        self._listeners.append(callback)

    def home(self) -> str | None:
        return self._fixed or self._current

    def tick(self, now: float) -> None:
        if self._tailer is None:
            return
        last = None
        for line in self._tailer.poll():
            m = LINE_RE.match(line)
            if m and m.group(7) == SYSTEM_SPEAKER and (c := SYSTEM_CHANGE_RE.match(m.group(8))):
                last = c.group(1)
        if last is not None:
            self._current = last
            log.info("current system: %s", last)
            for listener in self._listeners:
                listener(last)

    def status(self) -> str:
        return f"system {self.home() or '?'}"


@dataclass(frozen=True)
class IntelReport:
    when: datetime
    speaker: str
    system: str
    jumps: int
    kind: str
    tier: TierConfig
    text: str


class IntelAnalyzer:
    def __init__(self, cfg: IntelConfig, universe: Universe):
        self._cfg = cfg
        self._universe = universe
        self._ignore_words = {w.lower() for w in cfg.ignore_words}
        self._clear_words = {w.lower() for w in cfg.clear_words}
        self._own = {n.lower() for n in cfg.own_characters}
        self._ignored_systems = {s.lower() for s in cfg.ignore_systems}
        self._reach = max((t.max_jumps for t in cfg.tiers), default=-1)
        self._cache_home: str | None = None
        self._cache: dict[str, int] = {}

    def _distances(self, home: str) -> dict[str, int]:
        if home != self._cache_home:
            self._cache_home = home
            self._cache = self._universe.distances(home, self._reach)
        return self._cache

    def analyze(self, line: str, home: str | None, now_utc: datetime) -> IntelReport | None:
        m = LINE_RE.match(line)
        if not m or home is None or self._reach < 0:
            return None
        speaker, text = m.group(7), m.group(8)
        if speaker == SYSTEM_SPEAKER:
            return None
        if self._cfg.ignore_own_messages and speaker.lower() in self._own:
            return None
        year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
        when = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        max_age = self._cfg.max_message_age_sec
        if max_age > 0 and now_utc - when > timedelta(seconds=max_age):
            return None
        if text.lstrip().startswith("Kill:"):
            return None
        tokens = {t.lower() for t in TOKEN_RE.findall(text)}
        if tokens & self._ignore_words:
            return None
        kind = "clear" if tokens & self._clear_words else "report"
        if kind == "clear" and self._cfg.clear_mode == "ignore":
            return None
        found = self._universe.find_systems(text, self._cfg.aliases, self._ignored_systems)
        if not found:
            return None
        distances = self._distances(home)
        nearest = min((s for s in found if s in distances), key=lambda s: distances[s], default=None)
        if nearest is None:
            return None
        jumps = distances[nearest]
        tier = next((t for t in self._cfg.tiers if jumps <= t.max_jumps), None)
        if tier is None:
            return None
        return IntelReport(when, speaker, nearest, jumps, kind, tier, text)


Emit = Callable[[str, int, str], object]


class IntelWatcher:
    name = "intel"

    def __init__(
        self,
        cfg: IntelConfig,
        universe: Universe,
        tailers: list[ChatlogTailer],
        home: Callable[[], str | None],
        emit: Emit,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._cfg = cfg
        self._tailers = tailers
        self._home = home
        self._emit = emit
        self._utcnow = utcnow
        self._analyzer = IntelAnalyzer(cfg, universe)
        self._last_seen: dict[tuple[str, str], float] = {}
        self.last: IntelReport | None = None

    def tick(self, now: float) -> None:
        for tailer in self._tailers:
            for line in tailer.poll():
                self.handle_line(line, now)

    def handle_line(self, line: str, now: float) -> IntelReport | None:
        report = self._analyzer.analyze(line, self._home(), self._utcnow())
        if report is None:
            return None
        key = (report.system, report.kind)
        if now - self._last_seen.get(key, float("-inf")) < self._cfg.dedup_sec:
            return None
        self._last_seen[key] = now
        self.last = report
        if report.kind == "clear":
            sound, priority = self._cfg.clear_sound, CLEAR_PRIORITY
        else:
            sound, priority = report.tier.sound, REPORT_PRIORITY_BASE + max(0, 10 - report.jumps)
        log.info("intel %s %s %dj: %s", report.kind, report.system, report.jumps, report.text[:80])
        self._emit(sound, priority, f"intel:{report.system}:{report.jumps}j")
        return report

    def status(self) -> str:
        if self.last is None:
            return "intel -"
        tag = "clr" if self.last.kind == "clear" else "rep"
        return f"intel {tag} {self.last.system} {self.last.jumps}j"
