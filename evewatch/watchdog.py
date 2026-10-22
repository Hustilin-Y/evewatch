from __future__ import annotations

import logging

from .config import LocalConfig

log = logging.getLogger(__name__)


class ViewWatchdog:
    """Detects that the captured region no longer looks like the Local list.

    A frame is "bad" when any of these holds
    - a view anchor is configured and the reference fragment is no longer found,
    - background calibration exists and too many pixels match neither the
      background nor any standing class (window covered, scale changed), or
    - blind_on_empty is set and no icons are found although some were seen
      earlier in this system.
    A bad state lasting blind_after_sec means the view is lost; the signal then
    repeats every blind_repeat_sec.
    """

    def __init__(self, cfg: LocalConfig):
        self._cfg = cfg
        self.blind = False
        self._bad_since: float | None = None
        self._last_signal = float("-inf")
        self._had_icons = False

    def reset(self) -> None:
        self._had_icons = False
        self._bad_since = None

    def update(
        self,
        total_icons: int,
        foreign_fraction: float | None,
        now: float,
        anchor_ok: bool | None = None,
    ) -> bool:
        """Returns True when the blind signal should be played now."""
        if total_icons > 0:
            self._had_icons = True
        bad = (
            anchor_ok is False
            or (foreign_fraction is not None and foreign_fraction > self._cfg.blind_foreign_fraction)
            or (self._cfg.blind_on_empty and self._had_icons and total_icons == 0)
        )
        if not bad:
            if self.blind:
                log.info("Local view recovered")
            self.blind = False
            self._bad_since = None
            self._last_signal = float("-inf")
            return False
        if self._bad_since is None:
            self._bad_since = now
        if now - self._bad_since < self._cfg.blind_after_sec:
            return False
        if not self.blind:
            log.warning("Local view lost (no list-like content in the capture region)")
        self.blind = True
        if now - self._last_signal >= self._cfg.blind_repeat_sec:
            self._last_signal = now
            return True
        return False
