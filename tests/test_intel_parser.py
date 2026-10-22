import dataclasses
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evewatch.config import TierConfig, default_config
from evewatch.intel_watch import (
    CLEAR_PRIORITY,
    ChatlogTailer,
    IntelAnalyzer,
    IntelWatcher,
    SystemTracker,
)
from test_universe import make_universe

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 13, 10, 0, 0, tzinfo=timezone.utc)
TIERS = (TierConfig(0, "auto:intel_0"), TierConfig(2, "auto:intel_2"), TierConfig(5, "auto:intel_5"))


def intel_cfg(**kw):
    base = default_config(Path(".")).intel
    return dataclasses.replace(base, tiers=TIERS, channels=("Intel",), **kw)


def line(body, speaker="Pilot01", when="09:59:30"):
    return f"﻿[ 2026.10.13 {when} ] {speaker} > {body}"


def analyze(body, home="Obe", cfg=None, **line_kw):
    analyzer = IntelAnalyzer(cfg or intel_cfg(), make_universe())
    return analyzer.analyze(line(body, **line_kw), home, NOW)


def test_report_with_system_picks_tier():
    r = analyze("4-HWWF*  Zans Adolt")
    assert (r.system, r.jumps, r.kind, r.tier.sound) == ("4-HWWF", 1, "report", "auto:intel_2")


def test_distance_zero_and_far_tier():
    assert analyze("Obe  blue +5").tier.sound == "auto:intel_0"
    r = analyze("WH-JCA  Sisters probe")  # 4 jumps from Obe
    assert (r.jumps, r.tier.sound) == (4, "auto:intel_5")


def test_beyond_last_tier_is_ignored():
    assert analyze("Y-ZXIO  camp").jumps == 5  # exactly on the last tier boundary
    cfg = intel_cfg()
    cfg = dataclasses.replace(cfg, tiers=TIERS[:2])
    assert analyze("WH-JCA  camp", cfg=cfg) is None


def test_unreachable_system_is_ignored():
    assert analyze("0J3L-V  nv") is None


def test_nearest_of_several_systems_wins():
    r = analyze("WH-JCA  v  7-K5EL*  Pino Falcone")
    assert (r.system, r.jumps) == ("7-K5EL", 2)


def test_nv_is_a_regular_report():
    r = analyze("P3EN-E*  Evel0n nv")
    assert r.kind == "report" and r.system == "P3EN-E" and r.tier.sound == "auto:intel_5"


def test_clr_is_a_clear_signal_by_default():
    r = analyze("4-HWWF*  clr")
    assert r.kind == "clear" and r.jumps == 1


def test_clear_mode_ignore_drops_clr():
    assert analyze("4-HWWF*  clr", cfg=intel_cfg(clear_mode="ignore")) is None
    assert analyze("4-HWWF*  Clear", cfg=intel_cfg(clear_mode="ignore")) is None


def test_ignore_words_drop_message_entirely():
    assert analyze("4-HWWF*  spam", cfg=intel_cfg(ignore_words=("spam",))) is None


def test_substring_words_do_not_count_as_clear():
    r = analyze("4-HWWF*  clrx cleared")
    assert r.kind == "report"


def test_aliases_and_ignored_systems():
    cfg = intel_cfg(aliases={"pen": "P3EN-E"})
    assert analyze("pen camp", cfg=cfg).system == "P3EN-E"
    cfg = intel_cfg(ignore_systems=("4-HWWF",))
    assert analyze("4-HWWF  camp", cfg=cfg) is None


def test_own_character_ignored_only_when_enabled():
    cfg = intel_cfg(own_characters=("Pilot01",), ignore_own_messages=True)
    assert analyze("Obe  camp", cfg=cfg) is None
    assert analyze("Obe  camp", cfg=intel_cfg(own_characters=("Pilot01",))) is not None


def test_old_messages_are_dropped():
    assert analyze("Obe camp", when="09:50:00") is None  # 10 minutes old, limit 5
    assert analyze("Obe camp", when="09:57:00") is not None
    cfg = intel_cfg(max_message_age_sec=0)
    assert analyze("Obe camp", when="03:00:00", cfg=cfg) is not None


def test_system_speaker_kill_feed_and_unknown_home():
    assert analyze("Obe", speaker="EVE System") is None
    assert analyze("Kill: Somebody (Obe)") is None
    assert analyze("Obe camp", home=None) is None
    assert analyze("garbage line without header", home="Obe") is None


def test_no_system_in_text():
    assert analyze("hello everyone, any news?") is None


def test_urls_do_not_produce_systems():
    assert analyze("https://dscan.info/v/Obe123 look") is None


class Recorder:
    def __init__(self):
        self.items = []

    def __call__(self, sound, priority, label=""):
        self.items.append((sound, priority, label))


def make_watcher(home="Obe", **kw):
    rec = Recorder()
    w = IntelWatcher(intel_cfg(**kw), make_universe(), [], lambda: home, rec, utcnow=lambda: NOW)
    return w, rec


def test_watcher_emits_sound_and_closer_means_higher_priority():
    w, rec = make_watcher()
    w.handle_line(line("WH-JCA  camp"), 0.0)
    w.handle_line(line("Obe  camp"), 0.0)
    assert rec.items[0][0] == "auto:intel_5" and rec.items[1][0] == "auto:intel_0"
    assert rec.items[1][1] > rec.items[0][1]
    assert rec.items[1][2] == "intel:Obe:0j"


def test_watcher_dedups_same_system_within_window():
    w, rec = make_watcher(dedup_sec=30)
    for t in (0.0, 10.0, 29.0):
        w.handle_line(line("4-HWWF*  camp"), t)
    assert len(rec.items) == 1
    w.handle_line(line("4-HWWF*  camp again"), 31.0)
    assert len(rec.items) == 2


def test_watcher_clear_has_own_sound_priority_and_separate_dedup():
    w, rec = make_watcher()
    w.handle_line(line("4-HWWF*  camp"), 0.0)
    w.handle_line(line("4-HWWF*  clr"), 1.0)
    assert [i[0] for i in rec.items] == ["auto:intel_2", "auto:clear"]
    assert rec.items[1][1] == CLEAR_PRIORITY
    assert "clr" in w.status()


def test_watcher_status_before_and_after():
    w, rec = make_watcher()
    assert w.status() == "intel -"
    w.handle_line(line("4-HWWF*  camp"), 0.0)
    assert w.status() == "intel rep 4-HWWF 1j"


def test_fixture_end_to_end(tmp_path):
    shutil.copy(FIXTURES / "intel_sample.txt", tmp_path / "Intel_20261013_083850_1.txt")
    tailer = ChatlogTailer(tmp_path, "Intel", from_start=True)
    rec = Recorder()
    cfg = intel_cfg(max_message_age_sec=0, dedup_sec=0)
    w = IntelWatcher(cfg, make_universe(), [tailer], lambda: "Obe", rec, utcnow=lambda: NOW)
    w.tick(0.0)
    labels = [r[2] for r in rec.items]
    assert "intel:P3EN-E:3j" in labels
    assert "intel:4-HWWF:1j" in labels
    assert not any(l.startswith("intel:0J3L") for l in labels)


def test_system_tracker_reads_local_fixture(tmp_path):
    shutil.copy(FIXTURES / "local_sample.txt", tmp_path / "Local_20261013_083850_1.txt")
    tracker = SystemTracker(ChatlogTailer(tmp_path, "Local", from_start=True), "auto")
    seen = []
    tracker.subscribe(seen.append)
    assert tracker.home() is None
    tracker.tick(0.0)
    assert len(seen) == 1  # one notification per poll, with the last system
    assert tracker.home() == seen[0] and tracker.home()
    assert "system" in tracker.status()
    tracker.tick(1.0)
    assert len(seen) == 1


def test_system_tracker_follows_new_changes_and_fixed_home(tmp_path):
    from test_tailer import append, make_log, message

    path = make_log(tmp_path, "Local", "20261013_100000", text=message("10:00:01", "EVE System", "Channel changed to Local : Jita"))
    tracker = SystemTracker(ChatlogTailer(tmp_path, "Local", from_start=True), "auto")
    seen = []
    tracker.subscribe(seen.append)
    tracker.tick(0.0)
    append(path, message("10:05:00", "EVE System", "Channel changed to Local : New Caldari"))
    append(path, message("10:05:30", "Someone", "o/"))
    tracker.tick(1.0)
    assert seen == ["Jita", "New Caldari"]
    assert tracker.home() == "New Caldari"

    fixed = SystemTracker(ChatlogTailer(tmp_path, "Local", from_start=True), "Obe")
    fixed.tick(0.0)
    assert fixed.home() == "Obe"


@pytest.mark.parametrize("name", ["Jita", "New Caldari", "H-EY0P"])
def test_system_change_regex_accepts_spaces_and_digits(name):
    from evewatch.intel_watch import SYSTEM_CHANGE_RE

    assert SYSTEM_CHANGE_RE.match(f"Channel changed to Local : {name}").group(1) == name
