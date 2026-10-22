"""Opens the real tkinter windows for a moment; skipped where no display is available."""

import dataclasses
import subprocess
import sys
import tkinter as tk
from pathlib import Path

import numpy as np
import pytest

from helpers import RED, column, make_frame
from evewatch import calibrate
from evewatch.config import default_config
from evewatch.settings_gui import SettingsWindow


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    r.withdraw()
    yield r
    r.destroy()


def test_settings_window_roundtrips_config(root, tmp_path):
    cfg = default_config(tmp_path)
    cfg = dataclasses.replace(
        cfg, intel=dataclasses.replace(cfg.intel, channels=("Intel A", "Intel B"), aliases={"jt": "Jita"})
    )
    win = SettingsWindow(root, tmp_path / "config.toml", cfg)
    assert win.collect() == cfg


def test_button_bar_is_inside_the_window_on_every_tab(root, tmp_path):
    from tkinter import ttk

    win = SettingsWindow(root, tmp_path / "config.toml", default_config(tmp_path))
    root.deiconify()
    try:
        root.geometry("860x720+0+0")
        notebook = next(w for w in root.winfo_children() if isinstance(w, ttk.Notebook))
        for index in range(notebook.index("end")):
            notebook.select(index)
            root.update()
            bar = win.button_bar
            assert bar.winfo_ismapped()
            assert bar.winfo_y() + bar.winfo_height() <= root.winfo_height(), f"tab {index}: bar is cut off"
            labels = {c.cget("text") for c in bar.winfo_children() if "text" in c.keys()}
            assert "Select Local region on screen..." in labels
    finally:
        root.withdraw()


def test_settings_window_reports_invalid_input(root, tmp_path):
    win = SettingsWindow(root, tmp_path / "config.toml", default_config(tmp_path))
    win.vars["local.fps"].set("fast")
    with pytest.raises(Exception, match="fps"):
        win.collect()
    win.vars["local.fps"].set("0")
    with pytest.raises(Exception, match="fps"):
        win.collect()


def test_settings_window_custom_class_and_tier(root, tmp_path):
    win = SettingsWindow(root, tmp_path / "config.toml", default_config(tmp_path))
    win._new_class.set("Suspect")
    win._add_custom_class()
    win._add_tier_row(8, "auto:intel_8")
    cfg = win.collect()
    assert "suspect" in cfg.local.classes
    assert [t.max_jumps for t in cfg.intel.tiers] == [0, 2, 5, 8]


def test_settings_save_writes_file(root, tmp_path):
    path = tmp_path / "config.toml"
    win = SettingsWindow(root, path, default_config(tmp_path))
    win.vars["local.fps"].set("4")
    assert win.save()
    assert "fps = 4" in path.read_text(encoding="utf-8")


def test_select_region_window_opens_and_closes(root):
    assert calibrate.select_region(root, auto_close_ms=200) is None


def test_select_region_standalone_in_own_process():
    code = "from evewatch import calibrate; print(repr(calibrate.select_region(auto_close_ms=300)))"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "None"


def test_pick_colors_window_opens_and_closes(root, tmp_path):
    cfg = default_config(tmp_path)
    frame = make_frame(column(RED))
    assert calibrate.pick_colors(cfg, root, grab=lambda: frame, auto_close_ms=200) is None
