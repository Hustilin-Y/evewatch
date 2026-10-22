import os
import shutil
from pathlib import Path

from evewatch.intel_watch import ChatlogTailer, LINE_RE

FIXTURES = Path(__file__).parent / "fixtures"
HEADER = "\n\n        ---\n\n          Channel Name:    Test\n\n        ---\n\n"


def encode(text: str, bom: bool = True) -> bytes:
    return (b"\xff\xfe" if bom else b"") + text.encode("utf-16-le")


def message(when: str, speaker: str, body: str) -> str:
    return f"﻿[ 2026.10.13 {when} ] {speaker} > {body}\r\n"


def make_log(directory: Path, channel: str, stamp: str, char: str = "1", text: str = "") -> Path:
    path = directory / f"{channel}_{stamp}_{char}.txt"
    path.write_bytes(encode(HEADER + text))
    return path


def append(path: Path, text: str) -> None:
    with open(path, "ab") as fh:
        fh.write(text.encode("utf-16-le"))


def only_messages(lines):
    return [l for l in lines if LINE_RE.match(l)]


def tailer(directory, channel="Intel", **kw):
    clock = kw.pop("clock", None) or (lambda: 0.0)
    return ChatlogTailer(directory, channel, clock=clock, **kw)


def test_history_is_not_replayed_but_new_lines_are(tmp_path):
    path = make_log(tmp_path, "Intel", "20261013_100000", text=message("10:00:01", "A", "old one"))
    t = tailer(tmp_path)
    assert t.poll() == []
    append(path, message("10:01:00", "B", "P3EN-E*  camp"))
    lines = t.poll()
    assert len(lines) == 1 and LINE_RE.match(lines[0]).group(7) == "B"
    assert t.poll() == []


def test_from_start_reads_existing_content(tmp_path):
    make_log(tmp_path, "Local", "20261013_100000", text=message("10:00:01", "EVE System", "Channel changed to Local : Jita"))
    t = tailer(tmp_path, "Local", from_start=True)
    lines = only_messages(t.poll())
    assert [LINE_RE.match(l).group(8) for l in lines] == ["Channel changed to Local : Jita"]


def test_incomplete_line_is_buffered_until_newline(tmp_path):
    path = make_log(tmp_path, "Intel", "20261013_100000")
    t = tailer(tmp_path)
    t.poll()
    full = message("10:02:00", "B", "WH-JCA  clr")
    cut = len(full) // 2
    append(path, full[:cut])
    assert t.poll() == []
    append(path, full[cut:])
    lines = t.poll()
    assert len(lines) == 1 and lines[0].endswith("WH-JCA  clr")


def test_odd_byte_boundary_and_surrogate_pairs(tmp_path):
    path = make_log(tmp_path, "Intel", "20261013_100000")
    t = tailer(tmp_path)
    t.poll()
    raw = message("10:03:00", "B", "emoji \U0001F680 end").encode("utf-16-le")
    for i in range(0, len(raw), 3):  # 3-byte chunks split code units and surrogate pairs
        with open(path, "ab") as fh:
            fh.write(raw[i : i + 3])
        got = t.poll()
        if got:
            break
    assert got and got[0].endswith("emoji \U0001F680 end")
    assert "�" not in got[0]


def test_crlf_and_blank_lines_are_normalized(tmp_path):
    path = make_log(tmp_path, "Intel", "20261013_100000")
    t = tailer(tmp_path)
    t.poll()
    append(path, message("10:04:00", "B", "one") + "\r\n" + message("10:04:01", "C", "two"))
    lines = t.poll()
    assert len(lines) == 2 and not any(l.endswith("\r") for l in lines)


def test_empty_file_then_content(tmp_path):
    path = tmp_path / "Intel_20261013_100000_1.txt"
    path.write_bytes(b"")
    t = tailer(tmp_path)
    assert t.poll() == []
    path.write_bytes(encode(HEADER))
    assert only_messages(t.poll()) == []
    append(path, message("10:05:00", "B", "late start"))
    assert len(only_messages(t.poll())) == 1


def test_bom_only_file(tmp_path):
    path = tmp_path / "Intel_20261013_100000_1.txt"
    path.write_bytes(b"\xff\xfe")
    t = tailer(tmp_path)
    assert t.poll() == []
    append(path, message("10:05:00", "B", "x"))
    assert len(t.poll()) == 1


def test_new_session_file_is_followed_from_its_start(tmp_path):
    clock = [0.0]
    old = make_log(tmp_path, "Intel", "20261013_100000", text=message("10:00:01", "A", "old"))
    t = tailer(tmp_path, clock=lambda: clock[0])
    t.poll()
    new = make_log(tmp_path, "Intel", "20261013_120000", text=message("12:00:05", "B", "new session"))
    clock[0] = 11.0  # past the 10 s rescan interval
    lines = only_messages(t.poll())
    assert t.path == new
    assert [LINE_RE.match(l).group(8) for l in lines] == ["new session"]
    append(old, message("12:01:00", "A", "ignored: old file"))
    clock[0] = 25.0
    assert t.poll() == []


def test_rescan_only_after_interval(tmp_path):
    clock = [0.0]
    make_log(tmp_path, "Intel", "20261013_100000")
    t = tailer(tmp_path, clock=lambda: clock[0])
    t.poll()
    new = make_log(tmp_path, "Intel", "20261013_120000", text=message("12:00:05", "B", "x"))
    clock[0] = 5.0
    assert t.poll() == [] and t.path != new
    clock[0] = 10.5
    assert len(only_messages(t.poll())) == 1 and t.path == new


def test_truncated_file_is_reread(tmp_path):
    path = make_log(tmp_path, "Intel", "20261013_100000", text=message("10:00:01", "A", "a" * 200))
    t = tailer(tmp_path)
    t.poll()
    path.write_bytes(encode(HEADER + message("10:10:00", "B", "fresh")))
    assert path.stat().st_size < 2000
    lines = only_messages(t.poll())
    assert [LINE_RE.match(l).group(7) for l in lines] == ["B"]


def test_channel_name_must_match_exactly_and_newest_wins(tmp_path):
    make_log(tmp_path, "Intel", "20261013_090000", char="1")
    make_log(tmp_path, "Intel_Other", "20261013_150000", char="1")
    newest = make_log(tmp_path, "Intel", "20261013_110000", char="2")
    make_log(tmp_path, "Intel", "20261012_230000", char="1")
    (tmp_path / "notes.txt").write_text("x")
    t = tailer(tmp_path)
    t.poll()
    assert t.path == newest


def test_missing_directory_and_no_files(tmp_path):
    assert tailer(tmp_path / "nope").poll() == []
    assert tailer(tmp_path).poll() == []


def test_channel_name_with_special_characters(tmp_path):
    path = make_log(tmp_path, "wc.Vale+Tr+Ge", "20261013_083850", char="1234567890", text="")
    t = tailer(tmp_path, "wc.Vale+Tr+Ge")
    t.poll()
    append(path, message("08:40:00", "B", "ok"))
    assert len(t.poll()) == 1


def test_real_fixture_decodes_as_messages(tmp_path):
    shutil.copy(FIXTURES / "intel_sample.txt", tmp_path / "wc.Vale+Tr+Ge_20261013_083850_1.txt")
    t = tailer(tmp_path, "wc.Vale+Tr+Ge", from_start=True)
    lines = t.poll()
    parsed = [LINE_RE.match(l) for l in lines]
    messages = [m for m in parsed if m]
    assert len(messages) >= 50
    assert messages[0].group(7) == "EVE System"
    assert all(m.group(7).startswith("Pilot") for m in messages[1:])
    assert any("P3EN-E*" in m.group(8) for m in messages)
    assert os.path.getsize(tmp_path / "wc.Vale+Tr+Ge_20261013_083850_1.txt") > 4000
