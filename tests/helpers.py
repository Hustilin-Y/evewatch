import dataclasses
from pathlib import Path

import numpy as np

from evewatch.config import default_config

BG = (20, 20, 20)
RED = (200, 30, 30)
ORANGE = (230, 110, 30)
GREY = (150, 150, 150)
BLUE = (80, 160, 230)


def local_cfg(**overrides):
    cfg = default_config(Path("."))
    return dataclasses.replace(cfg.local, **overrides)


def with_class(local, name, **overrides):
    classes = dict(local.classes)
    classes[name] = dataclasses.replace(classes[name], **overrides)
    return dataclasses.replace(local, classes=classes)


def make_frame(icons, height=120, width=14, bg=BG):
    """icons: list of (top_row, height, color); an icon fills columns 2..width-3."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :] = bg
    for top, h, color in icons:
        frame[top : top + h, 2 : width - 2] = color
    return frame


def column(*colors, icon_h=10, gap=6, top=4):
    icons = []
    y = top
    for color in colors:
        icons.append((y, icon_h, color))
        y += icon_h + gap
    return icons
