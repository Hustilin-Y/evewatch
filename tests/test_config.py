from pathlib import Path

import pytest

from evewatch.config import ConfigError, default_config, load_config

ROOT = Path(__file__).resolve().parent.parent


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_example_config_loads_and_matches_defaults():
    example = load_config(ROOT / "config.example.toml")
    defaults = default_config(ROOT)
    assert example.local.classes == defaults.local.classes
    assert example.intel.tiers == defaults.intel.tiers
    assert example.local.fps == defaults.local.fps
    assert example.intel.ignore_words == () and example.intel.clear_words == ("clr", "clear")


def test_defaults():
    cfg = default_config(Path("x"))
    assert not cfg.local.calibrated
    assert [t.max_jumps for t in cfg.intel.tiers] == [0, 2, 5]
    enabled = {n for n, c in cfg.local.classes.items() if c.enabled}
    assert enabled == {"terrible", "bad", "neutral"}


def test_partial_override_keeps_other_defaults(tmp_path):
    cfg = load_config(write(tmp_path, "[local]\nfps = 4\n\n[local.classes.bad]\ntolerance = 10\n"))
    assert cfg.local.fps == 4.0
    assert cfg.local.classes["bad"].tolerance == 10
    assert cfg.local.classes["bad"].color == (230, 110, 30)
    assert cfg.local.confirm_frames == 2


def test_relative_paths_resolve_against_config_dir(tmp_path):
    cfg = load_config(write(tmp_path, ""))
    assert cfg.general.log_file == tmp_path / "evewatch.log"
    assert cfg.sounds.directory == tmp_path / "sounds_cache"


def test_custom_class(tmp_path):
    cfg = load_config(write(tmp_path, '[local.classes.suspect]\ncolor = [240, 220, 40]\nsound = "auto:tone_1000_2"\n'))
    assert cfg.local.classes["suspect"].color == (240, 220, 40)
    assert cfg.local.classes["suspect"].enabled


def test_custom_class_requires_color(tmp_path):
    with pytest.raises(ConfigError, match=r"suspect\] color: custom classes must define"):
        load_config(write(tmp_path, "[local.classes.suspect]\ntolerance = 5\n"))


def test_wrong_type_reports_line(tmp_path):
    path = write(tmp_path, '[general]\nlog_level = "INFO"\n\n[local]\nenabled = true\nfps = "fast"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert str(exc.value).startswith("config.toml:6: [local] fps: expected a finite number")


def test_unknown_key_reports_line(tmp_path):
    path = write(tmp_path, "[local]\nenabled = true\nfpz = 2\n")
    with pytest.raises(ConfigError, match=r"config\.toml:3: \[local\] fpz: unknown key"):
        load_config(path)


def test_unknown_section(tmp_path):
    with pytest.raises(ConfigError, match="mystery: unknown key"):
        load_config(write(tmp_path, "[mystery]\na = 1\n"))


def test_toml_syntax_error_mentions_line(tmp_path):
    with pytest.raises(ConfigError, match=r"invalid TOML.*line 2"):
        load_config(write(tmp_path, "[local]\nfps = = 2\n"))


def test_missing_file_message(tmp_path):
    with pytest.raises(ConfigError, match="config.example.toml"):
        load_config(tmp_path / "nope.toml")


def test_color_out_of_range(tmp_path):
    with pytest.raises(ConfigError, match=r"\[local.classes.bad\] color"):
        load_config(write(tmp_path, "[local.classes.bad]\ncolor = [300, 0, 0]\n"))


def test_invalid_sound_specs(tmp_path):
    with pytest.raises(ConfigError, match="unknown generated sound"):
        load_config(write(tmp_path, '[local.classes.bad]\nsound = "auto:nope"\n'))
    with pytest.raises(ConfigError, match=r"\.wav"):
        load_config(write(tmp_path, '[local.classes.bad]\nsound = "beep.mp3"\n'))


def test_wav_path_accepted(tmp_path):
    cfg = load_config(write(tmp_path, '[local.classes.bad]\nsound = "sounds/bad.wav"\n'))
    assert cfg.local.classes["bad"].sound == "sounds/bad.wav"


def test_tiers_must_increase_and_report_array_line(tmp_path):
    text = '[[intel.tiers]]\nmax_jumps = 3\nsound = "auto:intel_2"\n\n[[intel.tiers]]\nmax_jumps = 3\nsound = "auto:intel_5"\n'
    with pytest.raises(ConfigError, match=r"config\.toml:6: \[intel\.tiers\[1\]\] max_jumps: tiers must be"):
        load_config(write(tmp_path, text))


def test_tiers_replace_defaults(tmp_path):
    cfg = load_config(write(tmp_path, '[[intel.tiers]]\nmax_jumps = 1\nsound = "auto:intel_1"\n'))
    assert [t.max_jumps for t in cfg.intel.tiers] == [1]


def test_extra_links_and_aliases(tmp_path):
    text = '[intel]\nextra_links = [["A", "B"]]\naliases = { jita = "Jita", "4-4" = "Jita" }\n'
    cfg = load_config(write(tmp_path, text))
    assert cfg.intel.extra_links == (("A", "B"),)
    assert cfg.intel.aliases == {"jita": "Jita", "4-4": "Jita"}


def test_bad_extra_links(tmp_path):
    with pytest.raises(ConfigError, match="pair of system names"):
        load_config(write(tmp_path, '[intel]\nextra_links = [["A"]]\n'))


def test_bool_is_not_an_integer(tmp_path):
    with pytest.raises(ConfigError, match="expected an integer"):
        load_config(write(tmp_path, "[local]\nconfirm_frames = true\n"))


def test_region_may_have_negative_origin(tmp_path):
    cfg = load_config(write(tmp_path, "[local]\nregion = [-1900, -100, 40, 600]\n"))
    assert cfg.local.region == (-1900, -100, 40, 600)
    assert cfg.local.calibrated


def test_region_size_must_not_be_negative(tmp_path):
    with pytest.raises(ConfigError, match="width and height must be >= 0"):
        load_config(write(tmp_path, "[local]\nregion = [0, 0, -5, 10]\n"))


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_non_finite_numbers_are_rejected(tmp_path, value):
    with pytest.raises(ConfigError, match="finite number"):
        load_config(write(tmp_path, f"[local]\ncooldown_sec = {value}\n"))


def test_dotted_key_errors_report_line(tmp_path):
    text = "[local]\nenabled = true\nclasses.bad.tolerance = 999\n"
    with pytest.raises(ConfigError, match=r"config\.toml:3: \[local.classes.bad\] tolerance"):
        load_config(write(tmp_path, text))
