from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_GRAY = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def to_gray(patch: np.ndarray) -> np.ndarray:
    return patch.astype(np.float32) @ _GRAY


def ncc(patch: np.ndarray, reference: np.ndarray) -> float:
    """Normalized cross-correlation of two equally sized RGB patches, -1..1 (0 when either is flat)."""
    if patch.shape != reference.shape:
        return 0.0
    a = to_gray(patch)
    b = to_gray(reference)
    a -= a.mean()
    b -= b.mean()
    denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
    if denom < 1e-6:
        return 0.0
    return float((a * b).sum() / denom)


class ViewAnchor:
    """A small reference fragment of the Local window (e.g. the member-count icon above the list).

    It is present whenever the window is visible and unobstructed, and independent of what shows
    through a semi-transparent window, so a low correlation means the view is lost.
    """

    def __init__(self, region: tuple[int, int, int, int], reference: np.ndarray, threshold: float = 0.6):
        self.region = region
        self.reference = reference
        self.threshold = threshold

    def score(self, patch: np.ndarray) -> float:
        return ncc(patch, self.reference)

    def present(self, patch: np.ndarray) -> bool:
        return self.score(patch) >= self.threshold


def save_reference(path: Path, patch: np.ndarray) -> None:
    from PIL import Image

    tmp = path.with_name(path.name + ".tmp.png")
    Image.fromarray(patch.astype(np.uint8)).save(tmp, format="PNG")
    tmp.replace(path)


def load_reference(path: Path) -> np.ndarray | None:
    from PIL import Image

    try:
        with Image.open(path) as img:
            return np.asarray(img.convert("RGB")).copy()
    except (OSError, ValueError) as exc:
        log.warning("cannot read the view anchor image %s: %s", path, exc)
        return None
