from __future__ import annotations

import dataclasses
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import ttk
from typing import Callable

import numpy as np

from .anchor import save_reference
from .capture import ScreenGrabber, enable_dpi_awareness
from .columns import locate_window_parts
from .local_watch import ICON_COLUMN_MAX_WIDTH, region_warning
from .config import Config, ConfigError, load_config, save_config

BACKGROUND = "background"


def rect_from_points(x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int, int]:
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    return left, top, right - left + 1, bottom - top + 1


def window_geometry(width: int, height: int, left: int, top: int) -> str:
    # Tk reads "-1920" as "1920 px from the right edge"; "+-1920" is x = -1920.
    return f"{width}x{height}+{left}+{top}"


def estimate_color(frame: np.ndarray, x: int, y: int, seed_tolerance: int = 30) -> tuple[tuple[int, int, int], int]:
    """Mean color of the icon under the click and a tolerance that covers its spread."""
    h, w, _ = frame.shape
    x = min(max(x, 0), w - 1)
    y = min(max(y, 0), h - 1)
    seed = frame[y, x].astype(np.int16)
    close = np.abs(frame.astype(np.int16) - seed).max(axis=2) <= seed_tolerance
    top = y
    while top > 0 and close[top - 1].any():
        top -= 1
    bottom = y
    while bottom < h - 1 and close[bottom + 1].any():
        bottom += 1
    pixels = frame[top : bottom + 1][close[top : bottom + 1]].astype(np.int16)
    mean = pixels.mean(axis=0)
    spread = int(np.abs(pixels - mean).max())
    color = tuple(int(round(v)) for v in mean)
    return color, min(80, max(15, spread + 10))  # type: ignore[return-value]


def _window(master: tk.Misc | None) -> tk.Tk | tk.Toplevel:
    return tk.Tk() if master is None else tk.Toplevel(master)


def select_region(
    master: tk.Misc | None = None,
    auto_close_ms: int | None = None,
    prompt: str | None = None,
) -> tuple[int, int, int, int] | None:
    """Freezes the screen, lets the user drag a rectangle; returns it in virtual-screen coordinates."""
    from PIL import Image, ImageTk

    enable_dpi_awareness()
    grabber = ScreenGrabber()
    try:
        frame, left, top = grabber.grab_virtual_screen()
    finally:
        grabber.close()
    h, w, _ = frame.shape

    win = _window(master)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.geometry(window_geometry(w, h, left, top))
    photo = ImageTk.PhotoImage(Image.fromarray(frame), master=win)
    canvas = tk.Canvas(win, width=w, height=h, highlightthickness=0, cursor="crosshair")
    canvas.pack()
    canvas.create_image(0, 0, image=photo, anchor="nw")
    canvas.create_text(
        w // 2, 24, fill="yellow", font=("Segoe UI", 14, "bold"),
        text=prompt
        or "Drag a rectangle around the Local window (or just its member list). The icon column is found "
        "automatically. Esc cancels.",
    )
    state: dict = {"start": None, "rect": None, "result": None}

    def press(e):
        state["start"] = (e.x, e.y)
        if state["rect"]:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=2)

    def drag(e):
        if state["start"]:
            canvas.coords(state["rect"], state["start"][0], state["start"][1], e.x, e.y)

    def release(e):
        if not state["start"]:
            return
        x, y, rw, rh = rect_from_points(*state["start"], e.x, e.y)
        if rw >= 3 and rh >= 3:
            state["result"] = (x + left, y + top, rw, rh)
            win.destroy()

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", drag)
    canvas.bind("<ButtonRelease-1>", release)
    win.bind("<Escape>", lambda e: win.destroy())
    win.focus_force()
    if auto_close_ms is not None:
        win.after(auto_close_ms, win.destroy)
    win.wait_window()
    return state["result"]


def pick_colors(
    cfg: Config,
    master: tk.Misc | None = None,
    grab: Callable[[], np.ndarray] | None = None,
    auto_close_ms: int | None = None,
) -> Config | None:
    """Shows the captured Local region magnified; click an icon, choose its class.

    Returns an updated Config, or None if the user cancelled.
    """
    from PIL import Image, ImageTk

    enable_dpi_awareness()
    grabber = None
    if grab is None:
        grabber = ScreenGrabber()
        region = cfg.local.region
        grab = lambda: grabber.grab(region)  # noqa: E731
    try:
        frame = grab()
    except Exception:
        if grabber is not None:
            grabber.close()
        raise
    h, w, _ = frame.shape
    zoom = max(1, min(8, 600 // max(w, 1)))

    win = _window(master)
    win.title("Pick standing colors")
    picks: dict[str, tuple[tuple[int, int, int], int]] = {}
    state = {"frame": frame, "photo": None, "saved": False}

    left = ttk.Frame(win)
    left.pack(side="left", fill="both", expand=True)
    canvas = tk.Canvas(left, width=min(w * zoom, 700), height=min(h * zoom, 700), cursor="tcross")
    scroll = ttk.Scrollbar(left, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scroll.set)
    canvas.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    def render():
        img = Image.fromarray(state["frame"]).resize((w * zoom, h * zoom), Image.NEAREST)
        state["photo"] = ImageTk.PhotoImage(img, master=win)
        canvas.delete("all")
        canvas.create_image(0, 0, image=state["photo"], anchor="nw")
        canvas.configure(scrollregion=(0, 0, w * zoom, h * zoom))

    right = ttk.Frame(win, padding=8)
    right.pack(side="right", fill="y")
    ttk.Label(right, text="1. Select what you will click:").pack(anchor="w")
    targets = list(cfg.local.classes) + [BACKGROUND]
    listbox = tk.Listbox(right, height=len(targets), exportselection=False)
    for t in targets:
        listbox.insert("end", t)
    listbox.selection_set(0)
    listbox.pack(fill="x")
    ttk.Label(right, text="2. Click the icon (or empty background).").pack(anchor="w", pady=(8, 0))
    result_var = tk.StringVar(value="Nothing picked yet.")
    ttk.Label(right, textvariable=result_var, wraplength=240, justify="left").pack(anchor="w", pady=8)

    def on_click(e):
        x = int(canvas.canvasx(e.x)) // zoom
        y = int(canvas.canvasy(e.y)) // zoom
        sel = listbox.curselection()
        if not sel or not (0 <= x < w and 0 <= y < h):
            return
        target = targets[sel[0]]
        color, tol = estimate_color(state["frame"], x, y)
        picks[target] = (color, tol)
        result_var.set("\n".join(f"{n}: {c} +/-{t}" for n, (c, t) in picks.items()))
        if sel[0] + 1 < len(targets):
            listbox.selection_clear(0, "end")
            listbox.selection_set(sel[0] + 1)

    def refresh():
        state["frame"] = grab()
        render()

    def save():
        state["saved"] = True
        win.destroy()

    ttk.Button(right, text="Re-capture region", command=refresh).pack(fill="x", pady=2)
    ttk.Button(right, text="Save", command=save).pack(fill="x", pady=2)
    ttk.Button(right, text="Cancel", command=win.destroy).pack(fill="x", pady=2)
    canvas.bind("<Button-1>", on_click)
    render()
    if auto_close_ms is not None:
        win.after(auto_close_ms, win.destroy)
    win.wait_window()

    if grabber is not None:
        grabber.close()
    if not state["saved"] or not picks:
        return None
    classes = dict(cfg.local.classes)
    local = cfg.local
    for name, (color, tol) in picks.items():
        if name == BACKGROUND:
            local = dataclasses.replace(local, background_color=color, background_tolerance=tol)
        else:
            classes[name] = dataclasses.replace(classes[name], color=color, tolerance=tol)
    return dataclasses.replace(cfg, local=dataclasses.replace(local, classes=classes))


@dataclass
class Calibration:
    region: tuple[int, int, int, int]
    anchor_region: tuple[int, int, int, int] | None = None
    anchor_patch: np.ndarray | None = None
    notes: list[str] = field(default_factory=list)


def refine_selection(
    selection: tuple[int, int, int, int], frame: np.ndarray | None = None
) -> Calibration:
    """Narrows a rectangle around the Local window to its icon column and finds the view anchor.

    `frame` is the capture of `selection`; it is grabbed from the screen when omitted.
    A selection that is already narrow is kept as it is.
    """
    x, y, w, h = selection
    if frame is None:
        grabber = ScreenGrabber()
        try:
            frame = grabber.grab(selection)
        finally:
            grabber.close()
    if w <= ICON_COLUMN_MAX_WIDTH:
        return Calibration(selection, notes=["The selection is narrow; used as the icon column."])
    located = locate_window_parts(frame)
    if located is not None and located.icons < 2 and located.anchor is None:
        located = None  # a lone bright rectangle is not evidence of a member list
    if located is None:
        return Calibration(
            selection,
            notes=[
                "No standing icons were found inside the selection, so it was kept as is. "
                + (region_warning(selection) or "")
            ],
        )
    top = max(0, located.top - 2)
    column_x = x + located.x0 - 1
    cal = Calibration(
        (column_x, y + top, (located.x1 - located.x0 + 1) + 2, h - top),
        notes=[f"Icon column found at x={column_x}, width {located.x1 - located.x0 + 3} px."],
    )
    if located.anchor is not None:
        ax0, ay0, ax1, ay1 = located.anchor
        cal.anchor_region = (x + ax0, y + ay0, ax1 - ax0 + 1, ay1 - ay0 + 1)
        cal.anchor_patch = frame[ay0 : ay1 + 1, ax0 : ax1 + 1].copy()
        cal.notes.append("View anchor (member-count icon) found.")
    else:
        cal.notes.append("No view anchor found; lost-view control will use the other criteria only.")
    return cal


def apply_calibration(cfg: Config, cal: Calibration) -> Config:
    local = dataclasses.replace(cfg.local, region=cal.region)
    if cal.anchor_region is not None and cal.anchor_patch is not None:
        save_reference(cfg.local.anchor_file, cal.anchor_patch)
        local = dataclasses.replace(local, anchor_region=cal.anchor_region)
    return dataclasses.replace(cfg, local=local)


def calibrate_anchor_command(config_path: Path) -> int:
    cfg = load_config(config_path)
    region = select_region(
        prompt="Drag a small rectangle around the member-count icon (the little person) at the top of the "
        "Local member list. Esc cancels."
    )
    if region is None:
        print("Cancelled, config unchanged.")
        return 1
    grabber = ScreenGrabber()
    try:
        patch = grabber.grab(region)
    finally:
        grabber.close()
    save_reference(cfg.local.anchor_file, patch)
    save_config(dataclasses.replace(cfg, local=dataclasses.replace(cfg.local, anchor_region=region)), config_path)
    print(f"Saved the view anchor {list(region)} (reference image: {cfg.local.anchor_file.name})")
    return 0


def calibrate_region_command(config_path: Path) -> int:
    cfg = load_config(config_path)
    region = select_region()
    if region is None:
        print("Cancelled, config unchanged.")
        return 1
    cal = refine_selection(region)
    save_config(apply_calibration(cfg, cal), config_path)
    for note in cal.notes:
        print(note)
    print(f"Saved local.region = {list(cal.region)}"
          + (f" and anchor {list(cal.anchor_region)}" if cal.anchor_region else "")
          + f" to {config_path} (previous version kept as .bak-<timestamp>)")
    return 0


def calibrate_colors_command(config_path: Path) -> int:
    cfg = load_config(config_path)
    if not cfg.local.calibrated:
        raise ConfigError("local.region is not set; run calibrate-region first")
    updated = pick_colors(cfg)
    if updated is None:
        print("Cancelled, config unchanged.")
        return 1
    backup = save_config(updated, config_path)
    print(f"Colors saved to {config_path}" + (f" (previous version: {backup.name})" if backup else ""))
    return 0
