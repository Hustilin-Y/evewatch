from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from .config import ConfigError, default_config, load_config, save_config

MENU = """
  1  Run (watch Local and intel)
  2  Settings (select the Local window, colors, channels, sounds)
  3  Download / update the system graph (needed for intel; ~95 MB, once)
  4  Test sounds
  q  Quit
"""


def app_dir() -> Path:
    """Folder that holds config.toml, universe.json, logs: next to the exe, or the current folder."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def default_config_path() -> Path:
    return app_dir() / "config.toml"


def ensure_config(path: Path, say: Callable[[str], object] = print) -> bool:
    """Creates a default config when none exists. Returns True when one was created."""
    if path.exists():
        return False
    save_config(default_config(path.resolve().parent), path)
    say(f"Created {path.name} next to the program with default settings.")
    return True


def _graph_status(path: Path) -> str:
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        return f"config problem: {exc}"
    return "ready" if cfg.intel.universe_file.exists() else "missing (intel signals are off until it is downloaded)"


def menu(
    config_path: Path,
    dispatch: Callable[[list[str]], int],
    ask: Callable[[str], str] = input,
    say: Callable[[str], object] = print,
) -> int:
    """Interactive start menu for people who just double-click the program."""
    say("EVE Watch - passive Local and intel watcher for EVE Online")
    try:
        created = ensure_config(config_path, say)
    except (OSError, ConfigError) as exc:
        say(f"Cannot create {config_path}: {exc}")
        ask("Press Enter to exit ")
        return 1
    if created:
        say("First start: choose 2 to select your Local window and intel channel, 3 to download the system graph.")
    while True:
        say(f"\nConfig: {config_path}")
        say(f"System graph: {_graph_status(config_path)}")
        say(MENU)
        try:
            choice = ask("> ").strip().lstrip("\ufeff").strip().lower()
        except EOFError:
            return 0
        base = ["--config", str(config_path)]
        if choice == "1":
            dispatch(base + ["run"])
        elif choice == "2":
            dispatch(base + ["settings"])
        elif choice == "3":
            answer = ask(
                "This downloads CCP's static data once (~95 MB) from developers.eveonline.com. Continue? [y/N] "
            )
            if answer.strip().lower() in ("y", "yes"):
                dispatch(base + ["build-universe"])
        elif choice == "4":
            dispatch(base + ["test-sounds"])
        elif choice in ("q", "quit", "exit", "5"):
            return 0
        else:
            say("Please type 1, 2, 3, 4 or q.")
