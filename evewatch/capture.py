from __future__ import annotations

import ctypes
import logging

import numpy as np

log = logging.getLogger(__name__)


def enable_dpi_awareness() -> None:
    try:
        result = ctypes.windll.shcore.SetProcessDpiAwareness(2)
        if result in (0, -2147024891):  # S_OK, or E_ACCESSDENIED: already DPI aware
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        log.warning("could not enable DPI awareness; region coordinates may be offset on scaled displays")


class ScreenGrabber:
    """Grabs RGB frames with mss. Create and use it from one thread."""

    def __init__(self) -> None:
        self._sct = None

    def _mss(self):
        if self._sct is None:
            import mss

            self._sct = getattr(mss, "MSS", None) or mss.mss
            self._sct = self._sct()
        return self._sct

    def grab(self, region: tuple[int, int, int, int]) -> np.ndarray:
        x, y, w, h = region
        shot = self._mss().grab({"left": x, "top": y, "width": w, "height": h})
        return np.asarray(shot)[:, :, 2::-1].copy()

    def grab_virtual_screen(self) -> tuple[np.ndarray, int, int]:
        """Whole virtual desktop (all monitors) plus its top-left offset."""
        mon = self._mss().monitors[0]
        shot = self._mss().grab(mon)
        return np.asarray(shot)[:, :, 2::-1].copy(), mon["left"], mon["top"]

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None
