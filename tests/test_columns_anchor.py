import dataclasses
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from helpers import BG, BLUE, GREY, RED, column, make_frame, local_cfg
from evewatch.anchor import ViewAnchor, load_reference, ncc, save_reference
from evewatch.calibrate import apply_calibration, refine_selection
from evewatch.columns import detect_icon_column, locate_window_parts
from evewatch.config import default_config
from evewatch.local_watch import LocalWatcher

FIXTURES = Path(__file__).parent / "fixtures"


def real(name):
    return np.asarray(Image.open(FIXTURES / name).convert("RGB")).copy()


# ---- icon column and anchor location on real captures of the user's Local window
@pytest.mark.parametrize("name", ["local_window_3_icons.png", "local_window_5_icons.png"])
def test_locates_column_top_and_anchor_in_real_window(name):
    located = locate_window_parts(real(name))
    assert (located.x0, located.x1) == (213, 224)
    assert located.top == 65
    ax0, ay0, ax1, ay1 = located.anchor
    assert 213 <= ax0 <= 216 and 40 <= ay0 <= 42 and 226 <= ax1 <= 228 and 53 <= ay1 <= 55


def test_chat_only_area_has_no_icon_column():
    window = real("local_window_3_icons.png")
    chat = window[:, :200]  # the chat text half, without the member list
    assert detect_icon_column(chat) is None
    assert locate_window_parts(chat) is None


def test_synthetic_column_is_found():
    frame = np.zeros((200, 60, 3), np.uint8)
    frame[:, :] = BG
    for top, h, color in column(RED, GREY, BLUE, icon_h=12, gap=6):
        frame[top : top + h, 2:14] = color
    assert detect_icon_column(frame) == (2, 13)


def test_flat_dark_frame_has_no_column():
    assert detect_icon_column(np.full((100, 80, 3), 20, np.uint8)) is None


# ---- region refinement used by calibration
def test_refine_selection_narrows_whole_window_selection():
    frame = real("local_window_3_icons.png")
    cal = refine_selection((427, 41, 311, 550), frame)
    x, y, w, h = cal.region
    assert (x, w) == (427 + 213 - 1, 14)
    assert y == 41 + 63 and y + h == 41 + 550  # starts just above the first icon, keeps the bottom edge
    assert cal.anchor_region is not None and cal.anchor_patch is not None
    assert cal.anchor_patch.shape[:2] == (cal.anchor_region[3], cal.anchor_region[2])
    assert any("Icon column found" in n for n in cal.notes)


def test_refine_keeps_narrow_selection():
    cal = refine_selection((100, 100, 16, 300), np.zeros((300, 16, 3), np.uint8))
    assert cal.region == (100, 100, 16, 300) and cal.anchor_region is None


def test_refine_wide_selection_without_icons_is_kept_with_a_warning():
    cal = refine_selection((0, 0, 300, 100), np.full((100, 300, 3), 30, np.uint8))
    assert cal.region == (0, 0, 300, 100)
    assert "No standing icons" in cal.notes[0]


def test_apply_calibration_writes_reference_and_region(tmp_path):
    cfg = default_config(tmp_path)
    cal = refine_selection((427, 41, 311, 550), real("local_window_3_icons.png"))
    new = apply_calibration(cfg, cal)
    assert new.local.region == cal.region and new.local.anchor_region == cal.anchor_region
    reference = load_reference(cfg.local.anchor_file)
    assert reference is not None and np.array_equal(reference, cal.anchor_patch)


# ---- anchor correlation
def test_ncc_identical_and_unrelated():
    rng = np.random.default_rng(5)
    patch = rng.integers(0, 256, (14, 14, 3), dtype=np.uint8)
    other = rng.integers(0, 256, (14, 14, 3), dtype=np.uint8)
    assert ncc(patch, patch) == pytest.approx(1.0, abs=1e-6)
    assert abs(ncc(patch, other)) < 0.4
    assert ncc(patch, patch[:10]) == 0.0
    assert ncc(np.full((14, 14, 3), 7, np.uint8), patch) == 0.0  # a flat patch can never match


def test_anchor_survives_changing_background_but_not_a_different_picture():
    reference = real("local_window_3_icons.png")[41:55, 214:228]
    anchor = ViewAnchor((0, 0, 14, 14), reference, 0.6)
    assert anchor.present(reference)
    rng = np.random.default_rng(1)
    for _ in range(20):  # the scene behind the semi-transparent window changes
        bg_shift = rng.integers(-18, 19, reference.shape)
        noisy = np.clip(reference.astype(int) + bg_shift, 0, 255).astype(np.uint8)
        assert anchor.present(noisy), anchor.score(noisy)
    window = real("local_window_3_icons.png")
    for patch in (window[100:114, 20:34], window[300:314, 100:114], np.zeros((14, 14, 3), np.uint8)):
        assert not anchor.present(patch)


def test_reference_roundtrip_and_errors(tmp_path):
    patch = np.random.default_rng(2).integers(0, 256, (14, 12, 3), dtype=np.uint8)
    path = tmp_path / "anchor.png"
    save_reference(path, patch)
    assert np.array_equal(load_reference(path), patch)
    assert load_reference(tmp_path / "missing.png") is None
    (tmp_path / "bad.png").write_bytes(b"not a png")
    assert load_reference(tmp_path / "bad.png") is None


# ---- LocalWatcher integration: covering the anchor means "view lost", even with a transparent background
def test_watcher_reports_lost_view_when_anchor_disappears():
    reference = real("local_window_3_icons.png")[41:55, 214:228]
    emitted = []
    cfg = local_cfg(blind_after_sec=2, blind_repeat_sec=10)
    watcher = LocalWatcher(
        cfg,
        lambda: make_frame([]),
        lambda *a: emitted.append(a),
        "auto:blind",
        anchor=ViewAnchor((0, 0, 14, 14), reference, 0.6),
    )
    frame = make_frame(column(GREY))
    for i in range(4):
        watcher.process_frame(frame, i * 0.5, reference)
    assert not emitted and "view ok" in watcher.status()
    covered = np.random.default_rng(4).integers(0, 256, reference.shape, dtype=np.uint8)
    for i in range(10):
        watcher.process_frame(frame, 2.0 + i * 0.5, covered)
    assert [e[2] for e in emitted] == ["local:blind"]
    assert "LOST" in watcher.status()
    watcher.process_frame(frame, 9.0, reference)
    assert "view ok" in watcher.status()


def test_anchor_config_roundtrip_and_validation(tmp_path):
    import tomllib

    from evewatch.config import ConfigError, dump_toml, parse_config

    cfg = default_config(tmp_path)
    local = dataclasses.replace(cfg.local, anchor_region=(641, 82, 14, 14), anchor_threshold=0.7)
    full = dataclasses.replace(cfg, local=local)
    text = dump_toml(full)
    assert parse_config(tomllib.loads(text), text, "t", tmp_path) == full
    bad = '[local]\nanchor_region = [1, 2, 0, 5]\n'
    with pytest.raises(ConfigError, match="anchor_region: width and height must be >= 1"):
        parse_config(tomllib.loads(bad), bad, "t", tmp_path)


def test_lone_bright_rectangle_is_not_taken_for_an_icon_column():
    frame = np.zeros((300, 400, 3), np.uint8)
    frame[:, :] = (30, 30, 36)
    frame[120:132, 150:160] = (200, 200, 90)  # one stray 10x12 element somewhere in the UI
    located = locate_window_parts(frame)
    assert located is not None and located.icons == 1 and located.anchor is None
    cal = refine_selection((10, 20, 400, 300), frame)
    assert cal.region == (10, 20, 400, 300)
    assert "No standing icons" in cal.notes[0]


def test_real_window_reports_icon_count():
    assert locate_window_parts(real("local_window_3_icons.png")).icons == 3
