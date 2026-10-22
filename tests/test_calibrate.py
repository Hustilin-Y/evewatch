import numpy as np

from helpers import BG, RED, column, make_frame
from evewatch.calibrate import estimate_color, rect_from_points


def test_rect_from_points_normalizes_corners():
    assert rect_from_points(10, 20, 4, 8) == (4, 8, 7, 13)
    assert rect_from_points(5, 5, 5, 5) == (5, 5, 1, 1)


def test_estimate_color_returns_icon_mean():
    frame = make_frame(column(RED, (150, 150, 150)))
    color, tol = estimate_color(frame, 6, 8)
    assert color == RED
    assert tol == 15


def test_estimate_color_covers_noise():
    rng = np.random.default_rng(7)
    frame = make_frame(column(RED, (150, 150, 150))).astype(np.int16)
    frame[4:14, 2:12] += rng.integers(-12, 13, size=(10, 10, 3))
    frame = np.clip(frame, 0, 255).astype(np.uint8)
    color, tol = estimate_color(frame, 6, 8)
    assert all(abs(a - b) <= 5 for a, b in zip(color, RED))
    assert 15 <= tol <= 40
    diff = np.abs(frame[4:14, 2:12].astype(int) - np.array(color)).max()
    assert diff <= tol


def test_estimate_color_on_background():
    color, _ = estimate_color(make_frame(column(RED)), 0, 100)
    assert color == BG


def test_click_outside_is_clamped():
    color, _ = estimate_color(make_frame(column(RED)), 500, 500)
    assert color == BG


def test_window_geometry_handles_negative_origin():
    from evewatch.calibrate import window_geometry

    assert window_geometry(3840, 1080, -1920, 0) == "3840x1080+-1920+0"
    assert window_geometry(1920, 1080, 0, -1080) == "1920x1080+0+-1080"
