from __future__ import annotations

import numpy as np

ICON_MIN_SIZE = 8
ICON_MAX_HEIGHT = 16
ICON_MAX_WIDTH = 20
BRIGHT_MIN = 60


def _icon_rectangles(frame: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Axis-aligned bright rectangles of icon size: (x0, y0, x1, y1), inclusive."""
    bright = frame.max(axis=2) >= BRIGHT_MIN
    height, width = bright.shape
    rects: list[tuple[int, int, int, int]] = []
    active: dict[tuple[int, int], int] = {}
    for x in range(width + 1):
        runs: set[tuple[int, int]] = set()
        if x < width:
            column = bright[:, x]
            padded = np.concatenate(([False], column, [False]))
            edges = np.flatnonzero(np.diff(padded.astype(np.int8)))
            for start, end in zip(edges[::2], edges[1::2]):
                length = end - start
                if ICON_MIN_SIZE <= length <= ICON_MAX_HEIGHT:
                    runs.add((int(start), int(end - 1)))
        for key in list(active):
            if key not in runs:
                x0 = active.pop(key)
                if ICON_MIN_SIZE <= x - x0 <= ICON_MAX_WIDTH:
                    rects.append((x0, key[0], x - 1, key[1]))
        for key in runs:
            active.setdefault(key, x)
    return rects


def detect_icon_column(frame: np.ndarray) -> tuple[int, int] | None:
    """Finds the narrow column of standing icons inside a larger capture of the Local window.

    Returns (x_start, x_end) inclusive in frame coordinates, or None when no icon-like rectangles
    were found (e.g. a list that contains only your own, icon-less row).
    """
    rects = _icon_rectangles(frame)
    if not rects:
        return None
    groups: list[list[tuple[int, int, int, int]]] = []
    for rect in rects:
        for group in groups:
            if abs(group[0][0] - rect[0]) <= 2 and abs(group[0][2] - rect[2]) <= 2:
                group.append(rect)
                break
        else:
            groups.append([rect])
    best = max(groups, key=len)
    return min(r[0] for r in best), max(r[2] for r in best)


from dataclasses import dataclass


@dataclass(frozen=True)
class Located:
    x0: int
    x1: int
    top: int
    anchor: tuple[int, int, int, int] | None
    icons: int = 1


ANCHOR_MIN = 5
ANCHOR_MAX = 24
ANCHOR_BRIGHT = 150
ANCHOR_SEARCH_ABOVE = 40


def locate_window_parts(frame: np.ndarray) -> Located | None:
    """Icon column, the top of the member list and the member-count icon above it (the view anchor).

    `frame` is a capture of the whole Local window (or a rectangle containing the member list).
    """
    rects = _icon_rectangles(frame)
    if not rects:
        return None
    groups: list[list[tuple[int, int, int, int]]] = []
    for rect in rects:
        for group in groups:
            if abs(group[0][0] - rect[0]) <= 2 and abs(group[0][2] - rect[2]) <= 2:
                group.append(rect)
                break
        else:
            groups.append([rect])
    best = max(groups, key=len)
    x0, x1 = min(r[0] for r in best), max(r[2] for r in best)
    top = min(r[1] for r in best)

    height, width, _ = frame.shape
    ya, yb = max(0, top - ANCHOR_SEARCH_ABOVE), max(0, top - 6)
    xa, xb = max(0, x0 - 2), min(width, x1 + 5)
    anchor = None
    if yb > ya:
        window = frame[ya:yb, xa:xb]
        ys, xs = np.where(window.max(axis=2) >= ANCHOR_BRIGHT)
        if ys.size:
            w = int(xs.max() - xs.min() + 1)
            h = int(ys.max() - ys.min() + 1)
            if ANCHOR_MIN <= w <= ANCHOR_MAX and ANCHOR_MIN <= h <= ANCHOR_MAX:
                anchor = (xa + int(xs.min()) - 1, ya + int(ys.min()) - 1, xa + int(xs.max()) + 1, ya + int(ys.max()) + 1)
    return Located(x0, x1, top, anchor, len(best))
