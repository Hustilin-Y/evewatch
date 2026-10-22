from __future__ import annotations

import json
import math
import re
import shutil
import tomllib
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sounds import is_valid_auto_name


class ConfigError(Exception):
    pass


LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
PRIORITY_MAX = 50

# name: (color, tolerance, enabled, sound, priority)
BUILTIN_CLASSES: dict[str, tuple[tuple[int, int, int], int, bool, str, int]] = {
    "terrible": ((200, 30, 30), 40, True, "auto:terrible", 5),
    "bad": ((230, 110, 30), 40, True, "auto:bad", 4),
    "neutral": ((150, 150, 150), 30, True, "auto:neutral", 3),
    "good": ((80, 160, 230), 40, False, "auto:good", 2),
    "excellent": ((30, 70, 190), 40, False, "auto:excellent", 1),
}
DEFAULT_TIERS = ((0, "auto:intel_0"), (2, "auto:intel_2"), (5, "auto:intel_5"))

_ARRAY_RE = re.compile(r"^\s*\[\[\s*([A-Za-z0-9_.\-]+)\s*\]\]\s*(#.*)?$")
_TABLE_RE = re.compile(r"^\s*\[\s*([A-Za-z0-9_.\-]+)\s*\]\s*(#.*)?$")
_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_\-\"'.]+)\s*=")
_CLASS_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True)
class GeneralConfig:
    log_level: str
    log_file: Path


@dataclass(frozen=True)
class SoundsConfig:
    max_queue_age_sec: float
    directory: Path
    blind_sound: str
    fault_sound: str


@dataclass(frozen=True)
class ClassConfig:
    name: str
    color: tuple[int, int, int]
    tolerance: int
    enabled: bool
    sound: str
    priority: int


@dataclass(frozen=True)
class LocalConfig:
    enabled: bool
    region: tuple[int, int, int, int]
    fps: float
    min_icon_height: int
    confirm_frames: int
    cooldown_sec: float
    repeat_while_present_sec: float
    alert_on_arrival: bool
    blind_after_sec: float
    blind_repeat_sec: float
    min_row_pixels: int = 2
    merge_gap_rows: int = 1
    background_color: tuple[int, int, int] | None = None
    background_tolerance: int = 25
    blind_foreign_fraction: float = 0.6
    blind_on_empty: bool = False
    anchor_region: tuple[int, int, int, int] | None = None
    anchor_file: Path = Path("anchor.png")
    anchor_threshold: float = 0.6
    classes: dict[str, ClassConfig] = field(default_factory=dict)

    @property
    def calibrated(self) -> bool:
        return self.region[2] > 0 and self.region[3] > 0


@dataclass(frozen=True)
class TierConfig:
    max_jumps: int
    sound: str


@dataclass(frozen=True)
class IntelConfig:
    enabled: bool
    chatlog_dir: str
    channels: tuple[str, ...]
    home_system: str
    ignore_words: tuple[str, ...]
    dedup_sec: float
    ignore_own_messages: bool
    own_characters: tuple[str, ...]
    extra_links: tuple[tuple[str, str], ...]
    aliases: dict[str, str]
    tiers: tuple[TierConfig, ...]
    clear_words: tuple[str, ...] = ("clr", "clear")
    clear_mode: str = "signal"
    clear_sound: str = "auto:clear"
    ignore_systems: tuple[str, ...] = ()
    universe_file: Path = Path("universe.json")
    max_message_age_sec: float = 300.0


@dataclass(frozen=True)
class Config:
    base_dir: Path
    general: GeneralConfig
    sounds: SoundsConfig
    local: LocalConfig
    intel: IntelConfig


class _Source:
    def __init__(self, name: str, text: str):
        self.name = name
        self._lines: dict[tuple[str, str | None], int] = {}
        label = ""
        array_counts: dict[str, int] = {}
        for no, line in enumerate(text.splitlines(), 1):
            if m := _ARRAY_RE.match(line):
                idx = array_counts.get(m.group(1), 0)
                array_counts[m.group(1)] = idx + 1
                label = f"{m.group(1)}[{idx}]"
                self._lines[(label, None)] = no
            elif m := _TABLE_RE.match(line):
                label = m.group(1)
                self._lines[(label, None)] = no
            elif m := _KEY_RE.match(line):
                key = m.group(1)
                parts = [key.strip("\"'")] if key[0] in "\"'" else key.split(".")
                owner = ".".join(p for p in [label, *parts[:-1]] if p)
                self._lines.setdefault((owner, parts[-1]), no)

    def where(self, label: str, key: str | None) -> str:
        line = self._lines.get((label, key)) or self._lines.get((label, None))
        loc = f"{self.name}:{line}" if line else self.name
        if label and key:
            return f"{loc}: [{label}] {key}: "
        if label:
            return f"{loc}: [{label}]: "
        return f"{loc}: {key}: " if key else f"{loc}: "


class _Section:
    def __init__(self, src: _Source, data: Any, label: str):
        self.src = src
        self.label = label
        if not isinstance(data, dict):
            raise ConfigError(src.where(label, None) + "expected a table")
        self.data: dict[str, Any] = data
        self._seen: set[str] = set()

    def fail(self, key: str | None, message: str) -> ConfigError:
        return ConfigError(self.src.where(self.label, key) + message)

    def _raw(self, key: str, default: Any) -> Any:
        self._seen.add(key)
        return self.data.get(key, default)

    def boolean(self, key: str, default: bool) -> bool:
        value = self._raw(key, default)
        if not isinstance(value, bool):
            raise self.fail(key, f"expected true or false, got {value!r}")
        return value

    def integer(self, key: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
        value = self._raw(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise self.fail(key, f"expected an integer, got {value!r}")
        self._check_range(key, value, minimum, maximum)
        return value

    def number(self, key: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
        value = self._raw(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise self.fail(key, f"expected a finite number, got {value!r}")
        self._check_range(key, value, minimum, maximum)
        return float(value)

    def _check_range(self, key: str, value: float, minimum: float | None, maximum: float | None) -> None:
        if minimum is not None and value < minimum:
            raise self.fail(key, f"must be >= {minimum}, got {value}")
        if maximum is not None and value > maximum:
            raise self.fail(key, f"must be <= {maximum}, got {value}")

    def string(self, key: str, default: str, *, choices: tuple[str, ...] | None = None) -> str:
        value = self._raw(key, default)
        if not isinstance(value, str):
            raise self.fail(key, f"expected a string, got {value!r}")
        if choices is not None and value not in choices:
            raise self.fail(key, f"must be one of {', '.join(choices)}, got {value!r}")
        return value

    def strings(self, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = self._raw(key, list(default))
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise self.fail(key, "expected a list of strings")
        return tuple(value)

    def ints(self, key: str, default: tuple[int, ...], count: int, *, minimum: int, maximum: int | None = None) -> tuple[int, ...]:
        value = self._raw(key, list(default))
        if (
            not isinstance(value, list)
            or len(value) != count
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)
        ):
            raise self.fail(key, f"expected a list of {count} integers")
        for v in value:
            self._check_range(key, v, minimum, maximum)
        return tuple(value)

    def optional_ints(self, key: str, count: int, *, minimum: int, maximum: int) -> tuple[int, ...] | None:
        if self.data.get(key, []) == []:
            self._seen.add(key)
            return None
        return self.ints(key, (), count, minimum=minimum, maximum=maximum)

    def sound(self, key: str, default: str) -> str:
        value = self.string(key, default)
        if value.startswith("auto:"):
            if not is_valid_auto_name(value[5:]):
                raise self.fail(
                    key,
                    f"unknown generated sound {value!r} (use auto:<class>, auto:intel_<N>, "
                    "auto:blind, auto:fault or auto:tone_<Hz>_<pulses>)",
                )
        elif not value.lower().endswith(".wav"):
            raise self.fail(key, f"expected 'auto:<name>' or a path to a .wav file, got {value!r}")
        return value

    def raw_table(self, key: str) -> dict[str, Any]:
        value = self._raw(key, {})
        if not isinstance(value, dict):
            raise self.fail(key, "expected a table")
        return value

    def raw_list(self, key: str) -> list[Any] | None:
        value = self._raw(key, None)
        if value is not None and not isinstance(value, list):
            raise self.fail(key, "expected a list")
        return value

    def finish(self) -> None:
        for key in self.data:
            if key not in self._seen:
                raise self.fail(key, "unknown key")


def _parse_general(sec: _Section, base_dir: Path) -> GeneralConfig:
    level = sec.string("log_level", "INFO", choices=LOG_LEVELS)
    log_file = Path(sec.string("log_file", "evewatch.log"))
    sec.finish()
    return GeneralConfig(level, log_file if log_file.is_absolute() else base_dir / log_file)


def _parse_sounds(sec: _Section, base_dir: Path) -> SoundsConfig:
    age = sec.number("max_queue_age_sec", 5.0, minimum=0.1, maximum=60)
    directory = Path(sec.string("directory", "sounds_cache"))
    blind = sec.sound("blind_sound", "auto:blind")
    fault = sec.sound("fault_sound", "auto:fault")
    sec.finish()
    return SoundsConfig(age, directory if directory.is_absolute() else base_dir / directory, blind, fault)


def _parse_class(src: _Source, name: str, data: Any) -> ClassConfig:
    label = f"local.classes.{name}"
    sec = _Section(src, data, label)
    builtin = BUILTIN_CLASSES.get(name)
    if builtin is None:
        if not _CLASS_NAME_RE.fullmatch(name):
            raise sec.fail(None, "class name must be lowercase letters, digits and underscores")
        if "color" not in sec.data:
            raise sec.fail("color", "custom classes must define 'color'")
    color, tolerance, enabled, sound, priority = builtin or ((0, 0, 0), 40, True, f"auto:tone_700_1", 1)
    result = ClassConfig(
        name,
        sec.ints("color", color, 3, minimum=0, maximum=255),  # type: ignore[arg-type]
        sec.integer("tolerance", tolerance, minimum=0, maximum=255),
        sec.boolean("enabled", enabled),
        sec.sound("sound", sound),
        sec.integer("priority", priority, minimum=0, maximum=PRIORITY_MAX),
    )
    sec.finish()
    return result


def _parse_region(sec: _Section) -> tuple[int, int, int, int]:
    region = sec.ints("region", (0, 0, 0, 0), 4, minimum=-100_000, maximum=100_000)
    if region[2] < 0 or region[3] < 0:
        raise sec.fail("region", "width and height must be >= 0 (x and y may be negative on multi-monitor setups)")
    return region  # type: ignore[return-value]


def _parse_anchor_region(sec: _Section) -> tuple[int, int, int, int] | None:
    region = sec.optional_ints("anchor_region", 4, minimum=-100_000, maximum=100_000)
    if region is not None and (region[2] < 1 or region[3] < 1):
        raise sec.fail("anchor_region", "width and height must be >= 1")
    return region  # type: ignore[return-value]


def _parse_local(src: _Source, sec: _Section, base_dir: Path) -> LocalConfig:
    classes_raw = sec.raw_table("classes")
    names = list(BUILTIN_CLASSES) + [n for n in classes_raw if n not in BUILTIN_CLASSES]
    classes = {n: _parse_class(src, n, classes_raw.get(n, {})) for n in names}
    result = LocalConfig(
        enabled=sec.boolean("enabled", True),
        region=_parse_region(sec),
        fps=sec.number("fps", 2, minimum=0.1, maximum=30),
        min_icon_height=sec.integer("min_icon_height", 8, minimum=1),
        confirm_frames=sec.integer("confirm_frames", 2, minimum=1, maximum=20),
        cooldown_sec=sec.number("cooldown_sec", 3, minimum=0),
        repeat_while_present_sec=sec.number("repeat_while_present_sec", 0, minimum=0),
        alert_on_arrival=sec.boolean("alert_on_arrival", True),
        blind_after_sec=sec.number("blind_after_sec", 10, minimum=0),
        blind_repeat_sec=sec.number("blind_repeat_sec", 30, minimum=1),
        min_row_pixels=sec.integer("min_row_pixels", 2, minimum=1),
        merge_gap_rows=sec.integer("merge_gap_rows", 1, minimum=0, maximum=20),
        background_color=sec.optional_ints("background_color", 3, minimum=0, maximum=255),  # type: ignore[arg-type]
        background_tolerance=sec.integer("background_tolerance", 25, minimum=0, maximum=255),
        blind_foreign_fraction=sec.number("blind_foreign_fraction", 0.6, minimum=0.05, maximum=1),
        blind_on_empty=sec.boolean("blind_on_empty", False),
        anchor_region=_parse_anchor_region(sec),
        anchor_file=_resolve(sec.string("anchor_file", "anchor.png"), base_dir),
        anchor_threshold=sec.number("anchor_threshold", 0.6, minimum=0.1, maximum=0.99),
        classes=classes,
    )
    sec.finish()
    return result


def _parse_tiers(src: _Source, sec: _Section) -> tuple[TierConfig, ...]:
    raw = sec.raw_list("tiers")
    if raw is None:
        return tuple(TierConfig(j, s) for j, s in DEFAULT_TIERS)
    tiers: list[TierConfig] = []
    for i, item in enumerate(raw):
        tsec = _Section(src, item, f"intel.tiers[{i}]")
        if "max_jumps" not in tsec.data:
            raise tsec.fail("max_jumps", "missing required key")
        tier = TierConfig(
            tsec.integer("max_jumps", 0, minimum=0, maximum=100),
            tsec.sound("sound", "auto:intel_0"),
        )
        tsec.finish()
        if tiers and tier.max_jumps <= tiers[-1].max_jumps:
            raise tsec.fail("max_jumps", "tiers must be listed in strictly increasing order of max_jumps")
        tiers.append(tier)
    return tuple(tiers)


def _resolve(value: str, base_dir: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


def _parse_intel(src: _Source, sec: _Section, base_dir: Path) -> IntelConfig:
    links_raw = sec.raw_list("extra_links") or []
    links: list[tuple[str, str]] = []
    for pair in links_raw:
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(s, str) and s for s in pair)):
            raise sec.fail("extra_links", 'each link must be a pair of system names, e.g. [["A", "B"]]')
        links.append((pair[0], pair[1]))
    aliases = sec.raw_table("aliases")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in aliases.items()):
        raise sec.fail("aliases", "expected a table of alias = \"System name\"")
    result = IntelConfig(
        enabled=sec.boolean("enabled", True),
        chatlog_dir=sec.string("chatlog_dir", "auto"),
        channels=sec.strings("channels", ()),
        home_system=sec.string("home_system", "auto"),
        ignore_words=sec.strings("ignore_words", ()),
        dedup_sec=sec.number("dedup_sec", 30, minimum=0),
        ignore_own_messages=sec.boolean("ignore_own_messages", False),
        own_characters=sec.strings("own_characters", ()),
        extra_links=tuple(links),
        aliases=dict(aliases),
        tiers=_parse_tiers(src, sec),
        clear_words=sec.strings("clear_words", ("clr", "clear")),
        clear_mode=sec.string("clear_mode", "signal", choices=("signal", "ignore")),
        clear_sound=sec.sound("clear_sound", "auto:clear"),
        ignore_systems=sec.strings("ignore_systems", ()),
        universe_file=_resolve(sec.string("universe_file", "universe.json"), base_dir),
        max_message_age_sec=sec.number("max_message_age_sec", 300, minimum=0),
    )
    sec.finish()
    return result


def parse_config(data: dict[str, Any], text: str, source: str, base_dir: Path) -> Config:
    src = _Source(source, text)
    root = _Section(src, data, "")
    general = _parse_general(_Section(src, root.raw_table("general"), "general"), base_dir)
    sounds = _parse_sounds(_Section(src, root.raw_table("sounds"), "sounds"), base_dir)
    local = _parse_local(src, _Section(src, root.raw_table("local"), "local"), base_dir)
    intel = _parse_intel(src, _Section(src, root.raw_table("intel"), "intel"), base_dir)
    root.finish()
    return Config(base_dir, general, sounds, local, intel)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ConfigError(
            f"config file not found: {path}. Copy config.example.toml to config.toml and edit it."
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None
    return parse_config(data, text, path.name, path.resolve().parent)


def default_config(base_dir: str | Path) -> Config:
    return parse_config({}, "", "<defaults>", Path(base_dir))


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _list(items) -> str:
    return "[" + ", ".join(_q(i) if isinstance(i, str) else str(i) for i in items) + "]"


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _flag(value: bool) -> str:
    return "true" if value else "false"


def rel_path(path: Path, base_dir: Path) -> str:
    try:
        return path.relative_to(base_dir).as_posix()
    except ValueError:
        return str(path)


def dump_toml(cfg: Config) -> str:
    g, s, lo, i = cfg.general, cfg.sounds, cfg.local, cfg.intel
    out = [
        "# EVE Watch configuration (written by evewatch; see config.example.toml for the key reference).",
        "",
        "[general]",
        f"log_level = {_q(g.log_level)}",
        f"log_file = {_q(rel_path(g.log_file, cfg.base_dir))}",
        "",
        "[sounds]",
        f"max_queue_age_sec = {_num(s.max_queue_age_sec)}",
        f"directory = {_q(rel_path(s.directory, cfg.base_dir))}",
        f"blind_sound = {_q(s.blind_sound)}",
        f"fault_sound = {_q(s.fault_sound)}",
        "",
        "[local]",
        f"enabled = {_flag(lo.enabled)}",
        f"region = {_list(lo.region)}",
        f"fps = {_num(lo.fps)}",
        f"min_icon_height = {lo.min_icon_height}",
        f"min_row_pixels = {lo.min_row_pixels}",
        f"merge_gap_rows = {lo.merge_gap_rows}",
        f"confirm_frames = {lo.confirm_frames}",
        f"cooldown_sec = {_num(lo.cooldown_sec)}",
        f"repeat_while_present_sec = {_num(lo.repeat_while_present_sec)}",
        f"alert_on_arrival = {_flag(lo.alert_on_arrival)}",
        f"blind_after_sec = {_num(lo.blind_after_sec)}",
        f"blind_repeat_sec = {_num(lo.blind_repeat_sec)}",
        f"blind_foreign_fraction = {_num(lo.blind_foreign_fraction)}",
        f"blind_on_empty = {_flag(lo.blind_on_empty)}",
        f"background_color = {_list(lo.background_color or [])}",
        f"background_tolerance = {lo.background_tolerance}",
        f"anchor_region = {_list(lo.anchor_region or [])}",
        f"anchor_file = {_q(rel_path(lo.anchor_file, cfg.base_dir))}",
        f"anchor_threshold = {_num(lo.anchor_threshold)}",
    ]
    for c in lo.classes.values():
        out += [
            "",
            f"[local.classes.{c.name}]",
            f"color = {_list(c.color)}",
            f"tolerance = {c.tolerance}",
            f"enabled = {_flag(c.enabled)}",
            f"sound = {_q(c.sound)}",
            f"priority = {c.priority}",
        ]
    aliases = ", ".join(f"{_q(k)} = {_q(v)}" for k, v in i.aliases.items())
    out += [
        "",
        "[intel]",
        f"enabled = {_flag(i.enabled)}",
        f"chatlog_dir = {_q(i.chatlog_dir)}",
        f"channels = {_list(i.channels)}",
        f"home_system = {_q(i.home_system)}",
        f"ignore_words = {_list(i.ignore_words)}",
        f"clear_words = {_list(i.clear_words)}",
        f"clear_mode = {_q(i.clear_mode)}",
        f"clear_sound = {_q(i.clear_sound)}",
        f"ignore_systems = {_list(i.ignore_systems)}",
        f"universe_file = {_q(rel_path(i.universe_file, cfg.base_dir))}",
        f"max_message_age_sec = {_num(i.max_message_age_sec)}",
        f"dedup_sec = {_num(i.dedup_sec)}",
        f"ignore_own_messages = {_flag(i.ignore_own_messages)}",
        f"own_characters = {_list(i.own_characters)}",
        "extra_links = [" + ", ".join(_list(pair) for pair in i.extra_links) + "]",
        f"aliases = {{ {aliases} }}" if aliases else "aliases = {}",
    ]
    for t in i.tiers:
        out += ["", "[[intel.tiers]]", f"max_jumps = {t.max_jumps}", f"sound = {_q(t.sound)}"]
    return "\n".join(out) + "\n"


BACKUPS_KEPT = 5


def save_config(cfg: Config, path: str | Path) -> Path | None:
    """Writes the config atomically; keeps the last few versions as <name>.bak-<timestamp>."""
    path = Path(path)
    text = dump_toml(cfg)
    parse_config(tomllib.loads(text), text, path.name, cfg.base_dir)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S-%f}")
        shutil.copyfile(path, backup)
        for stale in sorted(path.parent.glob(f"{path.name}.bak-*"))[:-BACKUPS_KEPT]:
            stale.unlink(missing_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return backup
