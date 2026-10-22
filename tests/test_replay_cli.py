import dataclasses

import numpy as np
import pytest
from PIL import Image

from helpers import RED, column, make_frame
from evewatch.__main__ import main
from evewatch.config import default_config, save_config


def save_png(path, frame):
    Image.fromarray(frame).save(path)


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.toml"
    save_config(default_config(tmp_path), path)
    return path


def test_replay_prints_alert_for_new_enemy(tmp_path, config_path, capsys):
    shots = tmp_path / "shots"
    shots.mkdir()
    for i, frame in enumerate([make_frame([]), make_frame([]), make_frame(column(RED)), make_frame(column(RED))]):
        save_png(shots / f"f{i}.png", frame)
    assert main(["-c", str(config_path), "replay", str(shots)]) == 0
    out = capsys.readouterr().out
    assert "ALERT local:terrible auto:terrible" in out
    assert "f3.png" in out
    assert "4 frame(s), 1 alert(s)" in out


def test_replay_crop_uses_region(tmp_path, config_path, capsys):
    cfg_text = config_path.read_text(encoding="utf-8").replace("region = [0, 0, 0, 0]", "region = [30, 10, 14, 120]")
    config_path.write_text(cfg_text, encoding="utf-8")
    shots = tmp_path / "shots"
    shots.mkdir()
    for i, icons in enumerate([[], [], column(RED), column(RED)]):
        screen = np.zeros((200, 100, 3), dtype=np.uint8)
        screen[10:130, 30:44] = make_frame(icons)
        save_png(shots / f"s{i}.png", screen)
    assert main(["-c", str(config_path), "replay", str(shots), "--crop"]) == 0
    assert "1 alert(s)" in capsys.readouterr().out


def test_replay_missing_folder(config_path, tmp_path, capsys):
    assert main(["-c", str(config_path), "replay", str(tmp_path / "nope")]) == 1
    assert "replay folder not found" in capsys.readouterr().err
