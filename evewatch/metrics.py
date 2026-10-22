from __future__ import annotations

import csv
import ctypes
import gc
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

FIELDS = ["time", "uptime_s", "rss_mb", "peak_rss_mb", "cpu_pct", "cpu_total_s", "threads", "handles", "gc_objects", "extra"]


class _MemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def memory_mb() -> tuple[float, float]:
    """(working set, peak working set) of this process in MB; (0, 0) when unavailable."""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MemoryCounters), wintypes.DWORD]
        counters = _MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters.WorkingSetSize / 1048576, counters.PeakWorkingSetSize / 1048576
    except (AttributeError, OSError):
        pass
    return 0.0, 0.0


def handle_count() -> int:
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        count = wintypes.DWORD()
        if kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count)):
            return int(count.value)
    except (AttributeError, OSError):
        pass
    return 0


class MetricsLogger:
    """Appends one CSV row every `interval_sec`: memory, CPU, threads, handles, live objects."""

    def __init__(
        self,
        path: str | Path,
        interval_sec: float = 60.0,
        extra: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.path = Path(path)
        self._interval = interval_sec
        self._extra = extra
        self._clock = clock
        self._started = clock()
        self._last_wall = self._started
        self._last_cpu = time.process_time()
        self._next = self._started
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        self._file = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        if new_file:
            self._writer.writerow(FIELDS)
            self._file.flush()

    def tick(self, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        if now < self._next:
            return False
        self._next = now + self._interval
        self.sample(now)
        return True

    def sample(self, now: float) -> None:
        rss, peak = memory_mb()
        cpu_total = time.process_time()
        wall = now - self._last_wall
        cpu_pct = 100.0 * (cpu_total - self._last_cpu) / wall if wall >= 0.5 else 0.0
        self._last_wall, self._last_cpu = now, cpu_total
        self._writer.writerow(
            [
                datetime.now().isoformat(timespec="seconds"),
                round(now - self._started, 1),
                round(rss, 1),
                round(peak, 1),
                round(cpu_pct, 2),
                round(cpu_total, 1),
                threading.active_count(),
                handle_count(),
                len(gc.get_objects()),
                self._extra() if self._extra else "",
            ]
        )
        self._file.flush()

    def close(self) -> None:
        try:
            self._file.close()
        except OSError:
            pass


def summarize(path: str | Path) -> dict:
    """Verdict inputs for a metrics CSV: growth of memory, threads and handles after a warm-up period."""
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if len(rows) < 3:
        return {"samples": len(rows), "enough_data": False}
    warm = max(1, len(rows) // 10)
    body = rows[warm:] or rows
    rss = [float(r["rss_mb"]) for r in body]
    cpu = [float(r["cpu_pct"]) for r in body[1:]] or [0.0]
    hours = max((float(body[-1]["uptime_s"]) - float(body[0]["uptime_s"])) / 3600.0, 1e-9)
    threads = [int(r["threads"]) for r in body]
    handles = [int(r["handles"]) for r in body]
    objects = [int(r["gc_objects"]) for r in body]
    return {
        "samples": len(rows),
        "enough_data": True,
        "hours": round((float(rows[-1]["uptime_s"])) / 3600.0, 2),
        "rss_start_mb": rss[0],
        "rss_end_mb": rss[-1],
        "rss_max_mb": max(rss),
        "rss_growth_mb_per_hour": round((rss[-1] - rss[0]) / hours, 2),
        "cpu_avg_pct": round(sum(cpu) / len(cpu), 2),
        "cpu_max_pct": round(max(cpu), 2),
        "threads_min": min(threads),
        "threads_max": max(threads),
        "handles_growth": handles[-1] - handles[0],
        "gc_objects_growth": objects[-1] - objects[0],
    }
