import numpy as np

from helpers import BG, BLUE, GREY, ORANGE, RED, column, local_cfg, make_frame, with_class
from evewatch.local_watch import classify_frame, count_runs


def counts(icons, cfg=None, **frame_kw):
    cfg = cfg or local_cfg()
    return classify_frame(make_frame(icons, **frame_kw), cfg).counts


def test_counts_each_class():
    result = counts(column(RED, RED, GREY, ORANGE, BLUE, RED))
    assert result == {"terrible": 3, "bad": 1, "neutral": 1, "good": 1, "excellent": 0}


def test_empty_list():
    assert sum(counts([]).values()) == 0


def test_short_stripes_are_ignored():
    assert counts([(5, 7, RED)])["terrible"] == 0
    assert counts([(5, 8, RED)])["terrible"] == 1


def test_color_within_tolerance_counts():
    near = (RED[0] - 25, RED[1] + 20, RED[2] + 15)
    assert counts([(5, 10, near)])["terrible"] == 1
    far = (RED[0] - 60, RED[1], RED[2])
    assert counts([(5, 10, far)])["terrible"] == 0


def test_nearest_class_wins_when_tolerances_overlap():
    cfg = with_class(local_cfg(), "neutral", tolerance=120)
    result = counts([(5, 10, RED)], cfg)
    assert result["terrible"] == 1 and result["neutral"] == 0


def test_disabled_class_is_still_counted():
    cfg = with_class(local_cfg(), "terrible", enabled=False)
    assert counts([(5, 10, RED)], cfg)["terrible"] == 1


def test_merge_gap_glues_split_icon():
    split_by_one = [(5, 6, RED), (12, 6, RED)]  # one blank row between
    assert counts(split_by_one)["terrible"] == 1
    split_by_three = [(5, 8, RED), (16, 8, RED)]
    assert counts(split_by_three)["terrible"] == 2


def test_min_row_pixels_filters_noise():
    frame = make_frame([])
    frame[10:30, 5] = RED  # single-pixel-wide line
    assert classify_frame(frame, local_cfg()).counts["terrible"] == 0
    frame[10:30, 6] = RED
    assert classify_frame(frame, local_cfg()).counts["terrible"] == 1


def test_foreign_fraction_needs_background():
    frame = make_frame(column(RED, GREY))
    assert classify_frame(frame, local_cfg()).foreign_fraction is None
    result = classify_frame(frame, local_cfg(background_color=BG))
    assert result.foreign_fraction < 0.01


def test_foreign_fraction_high_for_unrelated_picture():
    rng = np.random.default_rng(1)
    noise = rng.integers(0, 256, size=(120, 14, 3), dtype=np.uint8)
    result = classify_frame(noise, local_cfg(background_color=BG))
    assert result.foreign_fraction > 0.6


def test_count_runs():
    rows = np.array([0, 1, 1, 1, 0, 0, 1, 1, 1, 1, 0, 1], dtype=bool)
    assert count_runs(rows, 3, 0) == 2
    assert count_runs(rows, 3, 2) == 1  # runs separated by two empty rows merge
    assert count_runs(rows, 5, 0) == 0
    assert count_runs(np.zeros(5, dtype=bool), 1, 0) == 0
