import dataclasses
import tomllib
from pathlib import Path

import pytest

from evewatch.config import ConfigError, default_config, dump_toml, load_config, parse_config, save_config


def roundtrip(cfg):
    text = dump_toml(cfg)
    return parse_config(tomllib.loads(text), text, "dump", cfg.base_dir)


def test_defaults_roundtrip(tmp_path):
    cfg = default_config(tmp_path)
    assert roundtrip(cfg) == cfg


def test_rich_config_roundtrip(tmp_path):
    cfg = default_config(tmp_path)
    classes = dict(cfg.local.classes)
    classes["suspect"] = dataclasses.replace(classes["bad"], name="suspect", color=(240, 220, 40), sound="mine/x.wav")
    local = dataclasses.replace(
        cfg.local, region=(10, 20, 30, 400), background_color=(18, 18, 20), classes=classes, fps=2.5
    )
    intel = dataclasses.replace(
        cfg.intel,
        channels=("Ёлка-интел", 'A "quoted" one'),
        aliases={"jt": "Jita", "4-4": "Jita"},
        extra_links=(("A", "B"), ("C", "D")),
        home_system="Amarr",
    )
    full = dataclasses.replace(cfg, local=local, intel=intel)
    assert roundtrip(full) == full


def test_background_color_empty_means_uncalibrated(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[local]\nbackground_color = []\n", encoding="utf-8")
    assert load_config(path).local.background_color is None


def test_background_color_validated(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[local]\nbackground_color = [1, 2]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="background_color: expected a list of 3 integers"):
        load_config(path)


def test_save_config_writes_backup_and_is_loadable(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("# my comment\n[local]\nfps = 3\n", encoding="utf-8")
    cfg = load_config(path)
    save_config(dataclasses.replace(cfg, local=dataclasses.replace(cfg.local, region=(1, 2, 3, 4))), path)
    backups = list(tmp_path.glob("config.toml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8").startswith("# my comment")
    reloaded = load_config(path)
    assert reloaded.local.region == (1, 2, 3, 4) and reloaded.local.fps == 3.0


def test_new_local_keys_have_defaults():
    local = default_config(Path(".")).local
    assert (local.min_row_pixels, local.merge_gap_rows, local.blind_on_empty) == (2, 1, False)
    assert local.background_color is None


def test_negative_region_roundtrip(tmp_path):
    cfg = default_config(tmp_path)
    cfg = dataclasses.replace(cfg, local=dataclasses.replace(cfg.local, region=(-1920, 40, 60, 800)))
    assert roundtrip(cfg) == cfg


def test_backups_are_kept_and_pruned(tmp_path):
    from evewatch import config as config_module

    path = tmp_path / "config.toml"
    cfg = default_config(tmp_path)
    save_config(cfg, path)
    for i in range(config_module.BACKUPS_KEPT + 3):
        backup = save_config(dataclasses.replace(cfg, local=dataclasses.replace(cfg.local, fps=2 + i)), path)
        assert backup is not None and backup.exists()
    assert len(list(tmp_path.glob("config.toml.bak-*"))) == config_module.BACKUPS_KEPT
    assert not list(tmp_path.glob("*.tmp"))


def test_new_intel_keys_roundtrip(tmp_path):
    cfg = default_config(tmp_path)
    intel = dataclasses.replace(
        cfg.intel,
        clear_mode="ignore",
        clear_words=("clr", "clear", "чисто"),
        clear_sound="mine.wav",
        ignore_systems=("Hek", "Ami"),
        max_message_age_sec=0.0,
        ignore_words=("spam",),
    )
    full = dataclasses.replace(cfg, intel=intel)
    assert roundtrip(full) == full


def test_intel_defaults(tmp_path):
    intel = default_config(tmp_path).intel
    assert intel.clear_mode == "signal" and intel.clear_sound == "auto:clear"
    assert intel.ignore_words == () and intel.clear_words == ("clr", "clear")
    assert intel.universe_file == tmp_path / "universe.json"
    assert intel.max_message_age_sec == 300.0


def test_clear_mode_is_validated(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[intel]\nclear_mode = "maybe"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="clear_mode: must be one of signal, ignore"):
        load_config(path)
