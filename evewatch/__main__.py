from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, default_config, load_config
from .launcher import default_config_path
from .sounds import BLIND_PRIORITY, FAULT_PRIORITY, SoundLibrary, WinsoundBackend


def _load(args: argparse.Namespace) -> Config:
    return load_config(args.config)


def cmd_run(args: argparse.Namespace) -> int:
    from .app import run

    return run(
        _load(args),
        Path(args.config).resolve(),
        silent=args.silent,
        metrics_path=args.metrics,
        metrics_interval=args.metrics_interval,
    )


def cmd_metrics_report(args: argparse.Namespace) -> int:
    from .metrics import summarize

    try:
        s = summarize(args.file)
    except OSError as exc:
        print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
        return 1
    if not s["enough_data"]:
        print(f"Only {s['samples']} sample(s); let the run go longer.")
        return 1
    checks = [
        ("duration", f"{s['hours']} h", True),
        ("RAM max <= 80 MB (v1 budget)", f"{s['rss_max_mb']} MB", s["rss_max_mb"] <= 80),
        ("RAM growth <= 2 MB/h after warm-up", f"{s['rss_growth_mb_per_hour']} MB/h", s["rss_growth_mb_per_hour"] <= 2),
        ("CPU average <= 2 % of one core", f"{s['cpu_avg_pct']} %", s["cpu_avg_pct"] <= 2),
        ("thread count stable", f"{s['threads_min']}..{s['threads_max']}", s["threads_min"] == s["threads_max"]),
        ("handles growth <= 20", f"{s['handles_growth']:+d}", s["handles_growth"] <= 20),
        ("python objects growth <= 20000", f"{s['gc_objects_growth']:+d}", s["gc_objects_growth"] <= 20000),
    ]
    for name, value, ok in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {value}")
    return 0 if all(ok for _, _, ok in checks) else 3


def cmd_test_sounds(args: argparse.Namespace) -> int:
    path = Path(args.config)
    if path.is_file():
        config = load_config(path)
    else:
        print(f"note: {path} not found, using built-in defaults")
        config = default_config(Path.cwd())

    entries: list[tuple[str, str, int]] = []
    for cls in config.local.classes.values():
        suffix = "" if cls.enabled else " (disabled)"
        entries.append((f"local.{cls.name}{suffix}", cls.sound, cls.priority))
    for tier in config.intel.tiers:
        entries.append((f"intel.<= {tier.max_jumps} jumps", tier.sound, 0))
    entries.append(("local.blind (lost view)", config.sounds.blind_sound, BLIND_PRIORITY))
    entries.append(("fault", config.sounds.fault_sound, FAULT_PRIORITY))

    library = SoundLibrary(config.sounds.directory, config.base_dir)
    backend = WinsoundBackend()
    for i, (label, spec, priority) in enumerate(entries, 1):
        print(f"[{i:2}/{len(entries)}] {label:<28} {spec:<22} priority {priority}")
        backend.play(library.resolve(spec))
        time.sleep(0.4)
    return 0


def cmd_settings(args: argparse.Namespace) -> int:
    from .settings_gui import run_settings

    return run_settings(Path(args.config))


def cmd_calibrate_region(args: argparse.Namespace) -> int:
    from .calibrate import calibrate_region_command

    return calibrate_region_command(Path(args.config))


def cmd_calibrate_anchor(args: argparse.Namespace) -> int:
    from .calibrate import calibrate_anchor_command

    return calibrate_anchor_command(Path(args.config))


def cmd_calibrate_colors(args: argparse.Namespace) -> int:
    from .calibrate import calibrate_colors_command

    return calibrate_colors_command(Path(args.config))


def _replay_chatlog(args: argparse.Namespace, config: Config) -> int:
    import dataclasses
    import re
    from datetime import datetime, timezone

    from .intel_watch import LINE_RE, IntelWatcher
    from .universe import Universe, UniverseError

    if not args.home:
        raise ConfigError("--home <system> is required with --chatlog")
    try:
        universe = Universe.load(config.intel.universe_file)
    except UniverseError as exc:
        raise ConfigError(str(exc)) from None
    universe.add_links(list(config.intel.extra_links))
    if universe.canonical(args.home) is None:
        raise ConfigError(f"unknown system for --home: {args.home}")
    try:
        text = Path(args.chatlog).read_bytes().decode("utf-16", errors="replace")
    except OSError as exc:
        raise ConfigError(f"cannot read {args.chatlog}: {exc}") from None
    cfg = dataclasses.replace(config.intel, max_message_age_sec=0.0)
    current = {"when": datetime.now(timezone.utc)}
    shown = []

    def emit(sound: str, priority: int, label: str) -> None:
        shown.append((current["when"], sound, priority, label))

    watcher = IntelWatcher(cfg, universe, [], lambda: universe.canonical(args.home), emit, utcnow=lambda: current["when"])
    lines = text.splitlines()
    parsed = 0
    for line in lines:
        m = LINE_RE.match(line)
        if not m:
            continue
        parsed += 1
        y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
        current["when"] = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
        before = len(shown)
        watcher.handle_line(line, current["when"].timestamp())
        if len(shown) > before:
            when, sound, priority, label = shown[-1]
            print(f"{when:%m-%d %H:%M:%S}  {label:<28} {sound:<16} prio {priority:<3} {m.group(8)[:60]}")
    print(f"{parsed} message(s), {len(shown)} signal(s) for home system {universe.canonical(args.home)}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from .local_watch import load_frames, replay_frames

    config = _load(args)
    if args.chatlog is not None:
        return _replay_chatlog(args, config)
    if args.folder is None:
        raise ConfigError("give a folder of screenshots, or --chatlog <file> --home <system>")
    if not args.folder.is_dir():
        raise ConfigError(f"replay folder not found: {args.folder}")
    crop = config.local.region if args.crop else None
    if args.crop and not config.local.calibrated:
        raise ConfigError("--crop needs local.region to be set")
    frames = load_frames(args.folder, crop)
    if not frames:
        print(f"No images (.png/.bmp/.jpg) in {args.folder}", file=sys.stderr)
        return 1
    alerts = 0
    for ev in replay_frames(config.local, frames, config.sounds.blind_sound):
        if ev.kind == "frame":
            if args.verbose:
                print(f"{ev.time:7.1f}s  {ev.frame:<30} {ev.detail}")
        else:
            alerts += 1
            print(f"{ev.time:7.1f}s  {ev.frame:<30} ALERT {ev.detail}")
    print(f"{len(frames)} frame(s), {alerts} alert(s)")
    return 0


def cmd_build_universe(args: argparse.Namespace) -> int:
    import tempfile

    from .universe import SDE_LATEST_URL, UniverseError, download_sde, universe_from_sde_zip

    config_path = Path(args.config)
    output = args.output
    if output is None:
        config = load_config(config_path) if config_path.is_file() else default_config(Path.cwd())
        output = config.intel.universe_file
    try:
        if args.zip is not None:
            source = Path(args.zip)
            universe = universe_from_sde_zip(source)
        else:
            print(f"Downloading CCP static data from {SDE_LATEST_URL}")
            with tempfile.TemporaryDirectory(prefix="evewatch-sde-") as tmp:
                source = Path(tmp) / "sde.zip"

                def progress(done: int, total: int | None) -> None:
                    shown = f"{done / 1e6:5.1f} MB" + (f" / {total / 1e6:.1f} MB" if total else "")
                    print(f"\r  {shown}", end="", flush=True)

                download_sde(SDE_LATEST_URL, source, progress)
                print()
                universe = universe_from_sde_zip(source)
    except UniverseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    universe.save(output)
    gates = sum(len(n) for n in universe._adj.values()) // 2
    print(f"Saved {len(universe)} systems and {gates} gate links (SDE build {universe.build}) to {output}")
    return 0


def _not_implemented(stage: int, what: str):
    def handler(args: argparse.Namespace) -> int:
        print(f"{what} is not implemented yet (planned for stage {stage}).", file=sys.stderr)
        return 2

    return handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evewatch", description="Passive Local and intel channel watcher for EVE Online."
    )
    parser.add_argument("--version", action="version", version=f"evewatch {__version__}")
    parser.add_argument(
        "-c", "--config", default=str(default_config_path()), help="path to config.toml (default: next to the program)"
    )
    sub = parser.add_subparsers(dest="command", required=False)

    run_parser = sub.add_parser("run", help="start watching")
    run_parser.add_argument("--silent", action="store_true", help="do not play sounds (for measurements)")
    run_parser.add_argument("--metrics", type=Path, help="append process metrics (RAM, CPU, threads...) to this CSV")
    run_parser.add_argument("--metrics-interval", type=float, default=60.0, help="seconds between metric rows")
    run_parser.set_defaults(handler=cmd_run)
    report = sub.add_parser("metrics-report", help="summarize a --metrics CSV against the resource budgets")
    report.add_argument("file", type=Path)
    report.set_defaults(handler=cmd_metrics_report)
    sub.add_parser("test-sounds", help="play every configured sound in turn").set_defaults(handler=cmd_test_sounds)
    sub.add_parser("settings", help="open the settings window").set_defaults(handler=cmd_settings)
    sub.add_parser("calibrate-region", help="select the Local icon column on screen").set_defaults(
        handler=cmd_calibrate_region
    )
    sub.add_parser("calibrate-anchor", help="select the reference icon used to detect a lost Local view").set_defaults(
        handler=cmd_calibrate_anchor
    )
    sub.add_parser("calibrate-colors", help="pick standing colors from the captured region").set_defaults(
        handler=cmd_calibrate_colors
    )
    build = sub.add_parser("build-universe", help="build universe.json from CCP static data (one-off download)")
    build.add_argument("--zip", type=Path, help="use an already downloaded SDE JSONL zip instead of downloading")
    build.add_argument("--output", type=Path, help="where to write universe.json (default: intel.universe_file)")
    build.set_defaults(handler=cmd_build_universe)
    replay = sub.add_parser("replay", help="run saved Local screenshots through the watcher without the game")
    replay.add_argument("folder", type=Path, nargs="?", help="folder with Local screenshots (PNG/BMP/JPG)")
    replay.add_argument("--chatlog", type=Path, help="run an intel chatlog file through the Intel watcher instead")
    replay.add_argument("--home", help="your system for --chatlog (distances are measured from it)")
    replay.add_argument("--crop", action="store_true", help="crop each image to local.region (full-screen shots)")
    replay.add_argument("-v", "--verbose", action="store_true", help="print the class counts of every frame")
    replay.set_defaults(handler=cmd_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        from .launcher import menu

        return menu(Path(args.config), main)
    try:
        return args.handler(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
