from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Any

from . import calibrate
from .local_watch import region_warning
from .config import (
    LOG_LEVELS,
    Config,
    ConfigError,
    default_config,
    load_config,
    parse_config,
    rel_path,
    save_config,
)
from .sounds import SoundLibrary, WinsoundBackend


def _text_lines(widget: tk.Text) -> list[str]:
    return [line.strip() for line in widget.get("1.0", "end").splitlines() if line.strip()]


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


class SettingsWindow:
    def __init__(self, root: tk.Tk, path: Path, cfg: Config):
        self.root = root
        self.path = path
        self.cfg = cfg
        self.vars: dict[str, tk.Variable] = {}
        self.class_rows: dict[str, dict[str, tk.Variable]] = {}
        self.tier_rows: list[dict[str, tk.Variable]] = []
        self._backend: WinsoundBackend | None = None
        self._backend_lock = threading.Lock()
        self._messages: queue.Queue[str] = queue.Queue()
        root.title(f"EVE Watch settings - {path.name}")
        root.geometry("860x720")
        root.minsize(640, 420)

        # the button bar and status line are packed first so a tall tab can never push them out of view
        self.status = tk.StringVar(value="")
        ttk.Label(root, textvariable=self.status).pack(side="bottom", fill="x", padx=8, pady=(0, 6))
        self.button_bar = ttk.Frame(root)
        self.button_bar.pack(side="bottom", fill="x", padx=8, pady=(4, 6))
        ttk.Button(self.button_bar, text="Save", command=self.save).pack(side="left")
        ttk.Button(self.button_bar, text="Select Local region on screen...", command=self.select_region).pack(
            side="left", padx=6
        )
        ttk.Button(self.button_bar, text="Pick colors from screen...", command=self.pick_colors).pack(side="left")
        ttk.Button(self.button_bar, text="Close", command=self.close).pack(side="right")

        notebook = ttk.Notebook(root)
        notebook.pack(side="top", fill="both", expand=True, padx=8, pady=(8, 0))
        self._build_local(self._tab(notebook, "Local"))
        self._build_classes(self._tab(notebook, "Standing classes"))
        self._build_intel(self._tab(notebook, "Intel"))
        self._build_sounds(self._tab(notebook, "Sounds & general"))
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_messages()

    # ---- layout helpers
    def _tab(self, notebook: ttk.Notebook, title: str) -> ttk.Frame:
        """A scrollable tab page; returns the inner frame to put widgets into."""
        outer = ttk.Frame(notebook)
        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=10)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def wheel(event) -> None:
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        notebook.add(outer, text=title)
        return inner

    def _field(self, parent, row: int, key: str, label: str, value: Any, width: int = 14, hint: str = "") -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        var = tk.StringVar(value=str(value))
        self.vars[key] = var
        ttk.Entry(parent, textvariable=var, width=width).grid(row=row, column=1, sticky="w", padx=6)
        if hint:
            ttk.Label(parent, text=hint, foreground="gray").grid(row=row, column=2, sticky="w")

    def _check(self, parent, row: int, key: str, label: str, value: bool) -> None:
        var = tk.BooleanVar(value=value)
        self.vars[key] = var
        ttk.Checkbutton(parent, text=label, variable=var).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)

    # ---- tabs
    def _build_local(self, tab: ttk.Frame) -> None:
        lo = self.cfg.local
        self._check(tab, 0, "local.enabled", "Enable Local watcher", lo.enabled)
        ttk.Label(tab, text="Region x, y, width, height").grid(row=1, column=0, sticky="w", pady=2)
        box = ttk.Frame(tab)
        box.grid(row=1, column=1, columnspan=2, sticky="w", padx=6)
        for i, v in enumerate(lo.region):
            var = tk.StringVar(value=str(v))
            self.vars[f"region.{i}"] = var
            ttk.Entry(box, textvariable=var, width=6).pack(side="left", padx=2)
        rows = [
            ("fps", "Frames per second", lo.fps, "capture rate"),
            ("min_icon_height", "Min icon height (px)", lo.min_icon_height, "shorter stripes are ignored"),
            ("min_row_pixels", "Min matching pixels per row", lo.min_row_pixels, ""),
            ("merge_gap_rows", "Merge gap (rows)", lo.merge_gap_rows, "glue stripes split by small gaps"),
            ("confirm_frames", "Confirm frames", lo.confirm_frames, "same state N frames in a row"),
            ("cooldown_sec", "Cooldown (s)", lo.cooldown_sec, "per class"),
            ("repeat_while_present_sec", "Repeat while present (s)", lo.repeat_while_present_sec, "0 = off"),
            ("blind_after_sec", "Lost-view after (s)", lo.blind_after_sec, ""),
            ("blind_repeat_sec", "Lost-view repeat (s)", lo.blind_repeat_sec, ""),
            ("blind_foreign_fraction", "Lost-view foreign fraction", lo.blind_foreign_fraction, "needs background color"),
            ("background_tolerance", "Background tolerance", lo.background_tolerance, ""),
        ]
        for i, (key, label, value, hint) in enumerate(rows, start=2):
            self._field(tab, i, f"local.{key}", label, value, hint=hint)
        n = len(rows) + 2
        bg = ",".join(map(str, lo.background_color)) if lo.background_color else ""
        self._field(tab, n, "local.background_color", "Background color R,G,B", bg, hint="empty = not calibrated")
        anchor = ",".join(map(str, lo.anchor_region)) if lo.anchor_region else ""
        self._field(tab, n + 3, "local.anchor_region", "View anchor x,y,w,h", anchor, hint="set automatically by region selection")
        self._field(tab, n + 4, "local.anchor_threshold", "View anchor threshold", lo.anchor_threshold, hint="0.1-0.99, lower = more tolerant")
        self._check(tab, n + 1, "local.alert_on_arrival", "Signal for enemies already present on arrival", lo.alert_on_arrival)
        self._check(tab, n + 2, "local.blind_on_empty", "Treat an empty list (after icons were seen) as lost view", lo.blind_on_empty)

    def _build_classes(self, tab: ttk.Frame) -> None:
        self._class_frame = ttk.Frame(tab)
        self._class_frame.pack(fill="x")
        for i, h in enumerate(("On", "Class", "Color R,G,B", "", "Tol.", "Sound", "", "Prio.")):
            ttk.Label(self._class_frame, text=h, font=("Segoe UI", 9, "bold")).grid(row=0, column=i, padx=3, sticky="w")
        for cls in self.cfg.local.classes.values():
            self._add_class_row(cls.name, cls.color, cls.tolerance, cls.enabled, cls.sound, cls.priority)
        add = ttk.Frame(tab)
        add.pack(fill="x", pady=10)
        ttk.Label(add, text="Custom class name:").pack(side="left")
        self._new_class = tk.StringVar()
        ttk.Entry(add, textvariable=self._new_class, width=16).pack(side="left", padx=6)
        ttk.Button(add, text="Add", command=self._add_custom_class).pack(side="left")
        ttk.Label(
            tab,
            text="Higher priority wins when several classes appear at once.\n"
            "Sound: auto:<name> (generated) or a .wav file path.",
            foreground="gray",
        ).pack(anchor="w")

    def _add_custom_class(self) -> None:
        name = self._new_class.get().strip().lower()
        if not name or name in self.class_rows:
            return
        self._add_class_row(name, (240, 220, 40), 40, True, "auto:tone_1000_2", 4)
        self._new_class.set("")

    def _add_class_row(self, name, color, tolerance, enabled, sound, priority) -> None:
        r = len(self.class_rows) + 1
        f = self._class_frame
        row = {
            "enabled": tk.BooleanVar(value=enabled),
            "color": tk.StringVar(value=",".join(map(str, color))),
            "tolerance": tk.StringVar(value=str(tolerance)),
            "sound": tk.StringVar(value=sound),
            "priority": tk.StringVar(value=str(priority)),
        }
        self.class_rows[name] = row
        ttk.Checkbutton(f, variable=row["enabled"]).grid(row=r, column=0)
        ttk.Label(f, text=name).grid(row=r, column=1, sticky="w", padx=3)
        ttk.Entry(f, textvariable=row["color"], width=12).grid(row=r, column=2, padx=3)
        swatch = tk.Label(f, width=3, relief="sunken")
        swatch.grid(row=r, column=3, padx=3)

        def refresh(*_):
            try:
                r_, g_, b_ = (int(p) for p in row["color"].get().split(","))
                swatch.configure(bg=f"#{r_:02x}{g_:02x}{b_:02x}")
            except ValueError:
                swatch.configure(bg="SystemButtonFace")

        def choose():
            _, hexcolor = colorchooser.askcolor(color=swatch.cget("bg"), parent=self.root)
            if hexcolor:
                row["color"].set(",".join(str(int(hexcolor[i : i + 2], 16)) for i in (1, 3, 5)))

        row["color"].trace_add("write", refresh)
        refresh()
        swatch.bind("<Button-1>", lambda e: choose())
        ttk.Entry(f, textvariable=row["tolerance"], width=5).grid(row=r, column=4, padx=3)
        ttk.Entry(f, textvariable=row["sound"], width=22).grid(row=r, column=5, padx=3)
        ttk.Button(f, text="Test", width=5, command=lambda: self.test_sound(row["sound"].get())).grid(row=r, column=6)
        ttk.Entry(f, textvariable=row["priority"], width=4).grid(row=r, column=7, padx=3)

    def _build_intel(self, tab: ttk.Frame) -> None:
        i = self.cfg.intel
        self._check(tab, 0, "intel.enabled", "Enable Intel watcher", i.enabled)
        self._field(tab, 1, "intel.chatlog_dir", "Chatlog folder", i.chatlog_dir, width=44, hint='"auto" = Documents\\EVE\\logs\\Chatlogs')
        ttk.Button(tab, text="Browse...", command=self._browse_chatlogs).grid(row=1, column=3)
        self._field(tab, 2, "intel.home_system", "Home system", i.home_system, width=24, hint='"auto" = from the Local chatlog')
        self._field(tab, 3, "intel.ignore_words", "Ignore words", ", ".join(i.ignore_words), width=44, hint="comma-separated")
        self._field(tab, 4, "intel.dedup_sec", "Dedup (s)", i.dedup_sec)
        self._check(tab, 5, "intel.ignore_own_messages", "Ignore messages from my own characters", i.ignore_own_messages)
        self._field(tab, 6, "intel.own_characters", "My characters", ", ".join(i.own_characters), width=44, hint="comma-separated")

        ttk.Label(tab, text="Intel channels (one per line)").grid(row=7, column=0, sticky="nw", pady=(8, 0))
        self.channels = tk.Text(tab, height=4, width=40)
        self.channels.grid(row=7, column=1, columnspan=2, sticky="w", padx=6, pady=(8, 0))
        self.channels.insert("1.0", "\n".join(i.channels))
        ttk.Label(tab, text="Aliases (alias = System)").grid(row=8, column=0, sticky="nw", pady=(8, 0))
        self.aliases = tk.Text(tab, height=3, width=40)
        self.aliases.grid(row=8, column=1, columnspan=2, sticky="w", padx=6, pady=(8, 0))
        self.aliases.insert("1.0", "\n".join(f"{k} = {v}" for k, v in i.aliases.items()))
        ttk.Label(tab, text="Extra links (System A, System B)").grid(row=9, column=0, sticky="nw", pady=(8, 0))
        self.links = tk.Text(tab, height=3, width=40)
        self.links.grid(row=9, column=1, columnspan=2, sticky="w", padx=6, pady=(8, 0))
        self.links.insert("1.0", "\n".join(f"{a}, {b}" for a, b in i.extra_links))

        ttk.Label(tab, text="Jump tiers").grid(row=10, column=0, sticky="nw", pady=(8, 0))
        self._tier_frame = ttk.Frame(tab)
        self._tier_frame.grid(row=10, column=1, columnspan=3, sticky="w", padx=6, pady=(8, 0))
        for t in i.tiers:
            self._add_tier_row(t.max_jumps, t.sound)
        buttons = ttk.Frame(tab)
        buttons.grid(row=11, column=1, sticky="w", padx=6)
        ttk.Button(buttons, text="Add tier", command=lambda: self._add_tier_row(0, "auto:intel_0")).pack(side="left")
        ttk.Button(buttons, text="Remove last", command=self._remove_tier_row).pack(side="left", padx=6)

        self._field(tab, 12, "intel.universe_file", "System graph file", rel_path(i.universe_file, self.cfg.base_dir), width=30,
                    hint="built by `build-universe`")
        ttk.Label(tab, text="Clear reports (clr)").grid(row=13, column=0, sticky="w", pady=2)
        mode = tk.StringVar(value=i.clear_mode)
        self.vars["intel.clear_mode"] = mode
        ttk.Combobox(tab, textvariable=mode, values=("signal", "ignore"), state="readonly", width=10).grid(
            row=13, column=1, sticky="w", padx=6
        )
        self._field(tab, 14, "intel.clear_words", "Clear words", ", ".join(i.clear_words), width=30, hint="comma-separated")
        self._field(tab, 15, "intel.clear_sound", "Clear sound", i.clear_sound, width=22)
        ttk.Button(tab, text="Test", command=lambda: self.test_sound(self.vars["intel.clear_sound"].get())).grid(row=15, column=2, sticky="w")
        self._field(tab, 16, "intel.max_message_age_sec", "Ignore messages older than (s)", i.max_message_age_sec, hint="0 = off")
        self._field(tab, 17, "intel.ignore_systems", "Never match systems", ", ".join(i.ignore_systems), width=30,
                    hint="comma-separated; for names that are also common words")

    def _add_tier_row(self, max_jumps: int, sound: str) -> None:
        row = {"max_jumps": tk.StringVar(value=str(max_jumps)), "sound": tk.StringVar(value=sound)}
        self.tier_rows.append(row)
        r = len(self.tier_rows)
        f = self._tier_frame
        ttk.Label(f, text="up to").grid(row=r, column=0)
        ttk.Entry(f, textvariable=row["max_jumps"], width=4).grid(row=r, column=1, padx=3)
        ttk.Label(f, text="jumps").grid(row=r, column=2)
        ttk.Entry(f, textvariable=row["sound"], width=22).grid(row=r, column=3, padx=3)
        ttk.Button(f, text="Test", width=5, command=lambda: self.test_sound(row["sound"].get())).grid(row=r, column=4)

    def _remove_tier_row(self) -> None:
        if self.tier_rows:
            self.tier_rows.pop()
            slaves = self._tier_frame.grid_slaves(row=len(self.tier_rows) + 1)
            for w in slaves:
                w.destroy()

    def _build_sounds(self, tab: ttk.Frame) -> None:
        s, g = self.cfg.sounds, self.cfg.general
        self._field(tab, 0, "sounds.max_queue_age_sec", "Drop queued sounds older than (s)", s.max_queue_age_sec)
        self._field(tab, 1, "sounds.blind_sound", "Lost-view sound", s.blind_sound, width=26)
        ttk.Button(tab, text="Test", command=lambda: self.test_sound(self.vars["sounds.blind_sound"].get())).grid(row=1, column=2)
        self._field(tab, 2, "sounds.fault_sound", "Error sound", s.fault_sound, width=26)
        ttk.Button(tab, text="Test", command=lambda: self.test_sound(self.vars["sounds.fault_sound"].get())).grid(row=2, column=2)
        ttk.Label(tab, text="Log level").grid(row=3, column=0, sticky="w", pady=2)
        level = tk.StringVar(value=g.log_level)
        self.vars["general.log_level"] = level
        ttk.Combobox(tab, textvariable=level, values=LOG_LEVELS, state="readonly", width=12).grid(row=3, column=1, sticky="w", padx=6)
        self._field(tab, 4, "general.log_file", "Log file", rel_path(g.log_file, self.cfg.base_dir), width=30)
        self._field(tab, 5, "sounds.directory", "Generated sounds folder", rel_path(s.directory, self.cfg.base_dir), width=30)

    # ---- actions
    def _browse_chatlogs(self) -> None:
        chosen = filedialog.askdirectory(parent=self.root, title="Chatlogs folder")
        if chosen:
            self.vars["intel.chatlog_dir"].set(chosen)

    def _num(self, key: str) -> int | float:
        return self._num_of(self.vars[key], key)

    def _color(self, text: str, label: str) -> list[int]:
        if not text.strip():
            return []
        try:
            return [int(p) for p in text.split(",")]
        except ValueError:
            raise ConfigError(f"{label}: expected R,G,B, got {text!r}") from None

    def collect(self) -> Config:
        v = self.vars
        local: dict[str, Any] = {
            "enabled": v["local.enabled"].get(),
            "region": [self._num(f"region.{i}") for i in range(4)],
            "alert_on_arrival": v["local.alert_on_arrival"].get(),
            "blind_on_empty": v["local.blind_on_empty"].get(),
            "background_color": self._color(v["local.background_color"].get(), "background color"),
            "anchor_region": self._color(v["local.anchor_region"].get(), "view anchor"),
            "anchor_threshold": self._num("local.anchor_threshold"),
            "anchor_file": rel_path(self.cfg.local.anchor_file, self.cfg.base_dir),
            "classes": {},
        }
        for key in (
            "fps", "min_icon_height", "min_row_pixels", "merge_gap_rows", "confirm_frames", "cooldown_sec",
            "repeat_while_present_sec", "blind_after_sec", "blind_repeat_sec", "blind_foreign_fraction",
            "background_tolerance",
        ):
            local[key] = self._num(f"local.{key}")
        for name, row in self.class_rows.items():
            local["classes"][name] = {
                "color": self._color(row["color"].get(), f"class {name} color"),
                "tolerance": self._num_of(row["tolerance"], f"class {name} tolerance"),
                "enabled": row["enabled"].get(),
                "sound": row["sound"].get().strip(),
                "priority": self._num_of(row["priority"], f"class {name} priority"),
            }
        aliases = {}
        for line in _text_lines(self.aliases):
            if "=" not in line:
                raise ConfigError(f"alias line {line!r}: expected 'alias = System'")
            alias, system = (p.strip() for p in line.split("=", 1))
            aliases[alias] = system
        links = []
        for line in _text_lines(self.links):
            parts = _csv(line)
            if len(parts) != 2:
                raise ConfigError(f"extra link {line!r}: expected 'System A, System B'")
            links.append(parts)
        intel = {
            "enabled": v["intel.enabled"].get(),
            "chatlog_dir": v["intel.chatlog_dir"].get().strip() or "auto",
            "channels": _text_lines(self.channels),
            "home_system": v["intel.home_system"].get().strip() or "auto",
            "ignore_words": _csv(v["intel.ignore_words"].get()),
            "dedup_sec": self._num("intel.dedup_sec"),
            "ignore_own_messages": v["intel.ignore_own_messages"].get(),
            "own_characters": _csv(v["intel.own_characters"].get()),
            "clear_mode": v["intel.clear_mode"].get(),
            "clear_words": _csv(v["intel.clear_words"].get()),
            "clear_sound": v["intel.clear_sound"].get().strip(),
            "max_message_age_sec": self._num("intel.max_message_age_sec"),
            "ignore_systems": _csv(v["intel.ignore_systems"].get()),
            "universe_file": v["intel.universe_file"].get().strip(),
            "extra_links": links,
            "aliases": aliases,
            "tiers": [
                {
                    "max_jumps": self._num_of(r["max_jumps"], "tier max_jumps"),
                    "sound": r["sound"].get().strip(),
                }
                for r in self.tier_rows
            ],
        }
        data = {
            "general": {"log_level": v["general.log_level"].get(), "log_file": v["general.log_file"].get().strip()},
            "sounds": {
                "max_queue_age_sec": self._num("sounds.max_queue_age_sec"),
                "directory": v["sounds.directory"].get().strip(),
                "blind_sound": v["sounds.blind_sound"].get().strip(),
                "fault_sound": v["sounds.fault_sound"].get().strip(),
            },
            "local": local,
            "intel": intel,
        }
        return parse_config(data, "", "settings", self.cfg.base_dir)

    @staticmethod
    def _num_of(var: tk.Variable, label: str) -> int | float:
        text = str(var.get()).strip()
        for cast in (int, float):
            try:
                return cast(text)
            except ValueError:
                pass
        raise ConfigError(f"{label}: expected a number, got {text!r}")

    def save(self) -> bool:
        try:
            cfg = self.collect()
            backup = save_config(cfg, self.path)
        except ConfigError as exc:
            messagebox.showerror("Cannot save", str(exc), parent=self.root)
            return False
        self.cfg = cfg
        suffix = f" (previous version: {backup.name})" if backup else ""
        self.status.set(f"Saved to {self.path}{suffix}")
        return True

    def close(self) -> None:
        try:
            changed = self.collect() != self.cfg
        except ConfigError:
            changed = True
        if changed:
            answer = messagebox.askyesnocancel(
                "Unsaved changes", "Save the changes before closing?", parent=self.root
            )
            if answer is None:
                return
            if answer and not self.save():
                return
        self.root.destroy()

    def _poll_messages(self) -> None:
        try:
            while True:
                self.status.set(self._messages.get_nowait())
        except queue.Empty:
            pass
        try:
            self.root.after(200, self._poll_messages)
        except tk.TclError:
            pass

    def _hide_and_settle(self) -> None:
        self.root.withdraw()
        self.root.update()
        time.sleep(0.3)

    def select_region(self) -> None:
        self._hide_and_settle()
        try:
            selection = calibrate.select_region(self.root)
        finally:
            self.root.deiconify()
        if not selection:
            return
        cal = calibrate.refine_selection(selection)
        for i, value in enumerate(cal.region):
            self.vars[f"region.{i}"].set(str(value))
        if cal.anchor_region is not None and cal.anchor_patch is not None:
            from .anchor import save_reference

            try:
                save_reference(self.cfg.local.anchor_file, cal.anchor_patch)
            except OSError as exc:
                self.status.set(f"Cannot save the anchor image: {exc}")
            else:
                self.vars["local.anchor_region"].set(",".join(str(v) for v in cal.anchor_region))
        if self.save():
            warning = region_warning(cal.region)
            self.status.set(" ".join(cal.notes) + " Saved; a running `run` picks it up automatically.")
            if warning:
                messagebox.showwarning("Region looks too wide", warning, parent=self.root)

    def pick_colors(self) -> None:
        try:
            cfg = self.collect()
        except ConfigError as exc:
            messagebox.showerror("Fix the settings first", str(exc), parent=self.root)
            return
        if not cfg.local.calibrated:
            messagebox.showinfo("Region needed", "Select the Local region first.", parent=self.root)
            return
        self._hide_and_settle()
        try:
            updated = calibrate.pick_colors(cfg, self.root)
        finally:
            self.root.deiconify()
        if updated is None:
            return
        for name, cls in updated.local.classes.items():
            row = self.class_rows[name]
            row["color"].set(",".join(map(str, cls.color)))
            row["tolerance"].set(str(cls.tolerance))
        bg = updated.local.background_color
        self.vars["local.background_color"].set(",".join(map(str, bg)) if bg else "")
        self.vars["local.background_tolerance"].set(str(updated.local.background_tolerance))
        if self.save():
            self.status.set("Colors saved. The running `run` picks them up automatically.")

    def test_sound(self, spec: str) -> None:
        def play() -> None:
            try:
                with self._backend_lock:
                    if self._backend is None:
                        self._backend = WinsoundBackend()
                    library = SoundLibrary(self.cfg.sounds.directory, self.cfg.base_dir)
                    self._backend.play(library.resolve(spec.strip()))
            except Exception as exc:  # noqa: BLE001 - surfaced in the status line
                self._messages.put(f"Cannot play {spec!r}: {exc}")

        threading.Thread(target=play, daemon=True).start()


def run_settings(path: Path) -> int:
    from .capture import enable_dpi_awareness

    enable_dpi_awareness()
    if Path(path).is_file():
        cfg = load_config(path)
    else:
        cfg = default_config(Path(path).resolve().parent)
    root = tk.Tk()
    SettingsWindow(root, Path(path), cfg)
    root.mainloop()
    return 0
