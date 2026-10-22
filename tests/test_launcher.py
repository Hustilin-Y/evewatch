from pathlib import Path

from evewatch.config import load_config
from evewatch.launcher import default_config_path, ensure_config, menu


def scripted(answers):
    it = iter(answers)
    return lambda prompt="": next(it)


def test_first_start_creates_config_and_menu_dispatches(tmp_path):
    path = tmp_path / "config.toml"
    calls, said = [], []
    code = menu(path, lambda argv: calls.append(argv) or 0, scripted(["2", "4", "bogus", "q"]), said.append)
    assert code == 0
    assert path.exists() and load_config(path).intel.channels == ()
    assert calls == [["--config", str(path), "settings"], ["--config", str(path), "test-sounds"]]
    assert any("Created config.toml" in s for s in said)
    assert any("Please type" in s for s in said)


def test_download_needs_explicit_confirmation(tmp_path):
    path = tmp_path / "config.toml"
    calls = []
    menu(path, lambda argv: calls.append(argv) or 0, scripted(["3", "n", "3", "y", "q"]), lambda s: None)
    assert calls == [["--config", str(path), "build-universe"]]


def test_run_item_and_status_of_missing_graph(tmp_path):
    path = tmp_path / "config.toml"
    calls, said = [], []
    menu(path, lambda argv: calls.append(argv) or 0, scripted(["1", "q"]), said.append)
    assert calls == [["--config", str(path), "run"]]
    assert any("missing" in s for s in said)


def test_existing_config_is_left_alone(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("# mine\n[local]\nfps = 3\n", encoding="utf-8")
    assert ensure_config(path, lambda s: None) is False
    assert path.read_text(encoding="utf-8").startswith("# mine")


def test_unwritable_location_is_reported(tmp_path):
    said = []
    blocker = tmp_path / "file"
    blocker.write_text("x")
    code = menu(blocker / "config.toml", lambda argv: 0, scripted([""]), said.append)
    assert code == 1 and any("Cannot create" in s for s in said)


def test_default_path_is_config_toml():
    assert default_config_path().name == "config.toml"


def test_end_of_input_and_bom_are_handled(tmp_path):
    path = tmp_path / "config.toml"
    calls = []

    def ask(prompt=""):
        raise EOFError

    assert menu(path, lambda argv: calls.append(argv) or 0, ask, lambda s: None) == 0 and calls == []
    answers = iter(["﻿q"])
    assert menu(path, lambda argv: 0, lambda p="": next(answers), lambda s: None) == 0
