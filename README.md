# EVE Watch

A passive console tool for Windows that sits next to the EVE Online client and plays sounds:

1. **Local watcher** - a pilot with a given standing color appeared in Local.
2. **Intel watcher** - a system within N jumps of you was named in an intel channel.

It only observes and signals. It never touches the game.

> **Status:** stage 4 of 4 (hardening) in progress; stages 1-3 done. The Local watcher, calibration, settings window, `replay`, the chatlog tailer,
> system tracking, `build-universe` and the Intel watcher are implemented. Until `universe.json` exists
> (run `build-universe` once) the Intel watcher stays off.

## What it does and does not do

It does not read or write game memory, inject code, hook the process, emulate input, install global
keyboard/mouse hooks, sniff network traffic, use ESI SSO, or store tokens. A static test
(`tests/test_forbidden_apis.py`) fails the build if such APIs appear in the code.

It may take screenshots of a screen region you select (like any screen recorder), read the text logs the
client writes to `Documents\EVE\logs`, and play sounds.

**Disclaimer:** the tool is passive, but CCP does not publish an explicit permission for screen capture by
third-party tools. Check the current EULA and third-party tool policy yourself before use.

## Install

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Python 3.11+ on Windows 10/11. Dependencies: `mss`, `numpy`, `Pillow` (for development also `pytest`).

## First-time setup (Local watcher)

The easiest way is the settings window:

```
.venv\Scripts\python -m evewatch settings
```

1. Put the Local window where you want it, make it tall enough, and keep it uncovered.
2. Click **Select Local region on screen...** and drag a thin rectangle around **only the column of standing
   icons** in the member list (about 12-20 px wide, from the first row to the bottom of the list), not the
   names and not the chat. Selecting the whole Local window counts chat text and message icons as pilots
   (the tool warns when the region is wider than 80 px). Coordinates are physical pixels, so Windows
   scaling and multi-monitor layouts work.
3. Click **Pick colors from screen...**: pick a class in the list, click an icon of that standing in the
   magnified picture; repeat for each class. Finally pick the class `background` and click an empty area.
   The tolerance is suggested from how much the icon's color varies. Both pickers save the config
   immediately, and a running `run` picks the change up within a couple of seconds (key `r` forces a reload).
   Skip the `background` pick if your Local window is semi-transparent (the default): the scene shows
   through below the list, so a background color would make every frame look foreign and trigger the
   lost-view signal.
4. Adjust the rest if you like and press **Save**.

The same steps are available from the console as `calibrate-region` and `calibrate-colors`.
Saving rewrites `config.toml` (comments are lost); the previous version is kept as
`config.toml.bak-<timestamp>` (the last 5 are kept), so a hand-annotated file is never lost.

## Usage

```
python -m evewatch [--config config.toml] <command>
```

| Command | Purpose | Stage |
|---|---|---|
| `settings` | window to edit all settings, test sounds, select region and colors | 2 |
| `run` | start watching | 2 (Intel: 3) |
| `test-sounds` | play every configured sound in turn with its label | 1 |
| `calibrate-region`, `calibrate-anchor`, `calibrate-colors` | console entry points for the calibration steps | 2 |
| `run --metrics FILE [--silent]` / `metrics-report FILE` | resource log of a run and its verdict against the budgets | 4 |
| `replay <folder> [--crop] [-v]` | run saved screenshots through the Local watcher, print alerts | 2 |
| `build-universe [--zip FILE]` | one-off download of CCP static data and build of `universe.json` (offline afterwards) | 3 |
| `replay --chatlog FILE --home SYSTEM` | run an intel chatlog through the Intel watcher, print the signals | 3 |

`replay` takes PNG/BMP/JPG files in name order, treated as consecutive frames at the configured `fps`.
Use cropped column images as they are, or full-screen shots with `--crop` (cuts `local.region` out of each).

Keys in the console window while running (no global hooks, console only):
`p` pause, `m` mute, `s` status, `r` reload config, `q` quit. `Ctrl+C` also exits cleanly.
Changes to `config.toml` (from `settings` or by hand) are reloaded automatically; an invalid file is
reported and the previous settings stay active.

## How the Local watcher decides

1. The region is captured `fps` times per second.
2. Each pixel is assigned to the nearest standing class whose color is within its tolerance (per channel).
3. A row belongs to a class when at least `min_row_pixels` pixels match; consecutive rows (gaps up to
   `merge_gap_rows` allowed) form one icon if they are at least `min_icon_height` tall.
4. A state is *stable* after `confirm_frames` identical frames in a row (protects against flicker).
5. When a stable state has **more** icons of an enabled class than the previous one, that class signals;
   if several classes grew, the one with the highest `priority` plays. `cooldown_sec` is per class;
   `repeat_while_present_sec` repeats while the class is present.
6. At program start the present enemies are the silent baseline. After a system change they signal as
   "present on arrival" (`alert_on_arrival`).

Lost view (window covered or closed) is described in "Lost-view control" below.

## Intel watcher

1. Put your intel channels' exact names in `intel.channels` (e.g. `wc.Vale+Tr+Ge`). Chatlogs must be enabled in
   the client; files are read from `Documents\EVE\logs\Chatlogs` (`chatlog_dir = "auto"` follows OneDrive).
2. Build the system graph once: `python -m evewatch build-universe` (downloads CCP's static data, then works offline).
3. `run`. The current system is read from the Local chatlog (`Channel changed to Local : <System>`); set
   `home_system` to a name to override it.

How a message is handled:
- Systems are found by name (case-insensitive, whole words, trailing `*` of chat links ignored, multi-word names
  like `New Caldari` supported, URLs skipped). Aliases from `[intel.aliases]` are expanded.
- If several systems are named, the nearest to you counts. It must be within the largest tier's `max_jumps`;
  the first tier that fits chooses the sound. Extra edges (Ansiblex, bridges) come from `extra_links`.
- `nv` ("no visual", ship unknown) is a normal report. `clr` / `clear` is a "clear" report: it plays
  `clear_sound` at the lowest priority (`clear_mode = "ignore"` drops it). `ignore_words` drops messages entirely.
- The same system is not signalled again within `dedup_sec` (clear reports are deduplicated separately).
- Chat lines older than `max_message_age_sec` (log time is UTC), `Kill:` feed lines and system messages are skipped.
- Closer systems get a higher sound priority than farther ones.

Chatlog format (verified on real files): UTF-16 LE with BOM, message lines `[ 2026.10.13 08:39:23 ] Name > text`
each starting with U+FEFF, CRLF line ends, header with `Channel Name` / `Listener` / `Session started`.
New session files are picked up within 10 s; history is not replayed at start.

## Lost-view control

Everything the Local watcher knows comes from pixels, so if the Local window is closed, covered by another
window, moved or resized, it would see "no enemies" and stay silent - a dangerous silence. The lost-view
control exists to say "I am blind" instead. After `blind_after_sec` of a bad view it plays `blind_sound`
(highest priority) and repeats every `blind_repeat_sec`; class alerts are suppressed meanwhile.

A frame counts as bad when any of these holds:

1. **View anchor lost (recommended, works with a transparent window).** The calibration stores a small
   reference picture of a fixed element above the member list (the member-count icon) in `anchor.png`.
   Each frame that spot is compared with it by normalized correlation; the scene showing through a
   semi-transparent window does not matter. Below `anchor_threshold` (0.6) the view is lost.
2. A background color is calibrated and more than `blind_foreign_fraction` of the region is neither the
   background nor a standing color. Only useful for an opaque Local window.
3. `blind_on_empty` is on (off by default) and no icons at all are found after some were seen.

Selecting the Local window in `settings` ("Select Local region on screen...") finds the icon column, the
top of the list and the anchor automatically. If the anchor is not found, select it by hand with
`calibrate-anchor`.

## Measured performance (this machine, real screen and chatlogs, silent run, 14 x 488 px region, 2 fps)

| | measured | budget |
|---|---|---|
| CPU, average / peak | 0.7 % / 1.25 % of one core | 2 % |
| RAM | 54-57 MB | 80 MB |
| threads / handle growth | 2 / none | stable |
| network calls in `run` | 0 (tested with sockets disabled) | 0 |
| signal latency (accelerated test) | Local <= 1.5 s, Intel <= 1.5 s | 1.5 s |

Reproduce: `python tools/measure.py --seconds 120`. For long runs use
`python -m evewatch run --metrics metrics.csv` and then `python -m evewatch metrics-report metrics.csv`.

## Acceptance checklist

Automatic (run by `pytest`):

- [x] forbidden APIs absent (`tests/test_forbidden_apis.py`)
- [x] classifier on synthetic frames and on real captures of the Local window
- [x] state machine: growth, flicker, cooldown with deferral, arrival, system change, lost view
- [x] intel parser on real-format chatlogs: systems, links with `*`, aliases, `clr`, several systems, duplicates
- [x] BFS distances, `extra_links`, unreachable systems; graph built from CCP static data
- [x] chatlog tailer: UTF-16, incomplete lines, new file, truncation, empty file
- [x] no network access during `run`; no memory growth in an accelerated soak; latency budgets

Manual (on the live client):

1. [ ] Calibrate region and colors with `settings`; the status shows the icon counts matching the list.
2. [ ] A pilot of each enabled standing appearing in Local plays the right sound.
3. [ ] Covering the Local window (or closing it) plays the lost-view sound within `blind_after_sec`.
4. [ ] Intel messages naming systems 0, 1, 2 and 5 jumps away play different sounds; `clr` plays the soft one.
5. [ ] Jumping to another system resets the baseline (arrival signal for enemies already present).
6. [ ] 8-hour run: `run --metrics metrics.csv`, then `metrics-report metrics.csv` shows all PASS.

## Troubleshooting

- **No reaction from Local.** Check that the region is only the icon column (the app warns when wider than
  80 px) and that the colors were saved. `settings` saves after each picker; a running `run` reloads the
  config by itself (or press `r`). Look at the status line: the counts per class should match the list.
- **Chat text counted as pilots.** The region includes the chat; re-select the Local window and let the
  icon column be found automatically.
- **"Lost view" all the time.** A background color was calibrated for a semi-transparent window; clear
  `background_color` (empty list) and rely on the view anchor.
- **Nothing from Intel.** `channels` must equal the channel name in the chatlog file name
  (`<channel>_<date>_<time>_<id>.txt`); run `build-universe` once; `replay --chatlog FILE --home SYSTEM`
  shows what would be signalled for a saved log.
- **A word is taken for a system** (for example `Pain`, `Access`): add it to `ignore_systems`.
- **No sound.** Mute (`m`) is off? `test-sounds` plays everything; custom `.wav` files fall back to a beep.

## Sounds

`sound = "auto:<name>"` means a generated tone, cached in `sounds_cache/` (delete the folder to regenerate).
Any other value is a path to a `.wav` file. If a file cannot be played the tool falls back to `winsound.Beep`.

| Name | Meaning |
|---|---|
| `auto:terrible`, `bad`, `neutral`, `good`, `excellent` | Local classes. More dangerous = higher pitch and more pulses |
| `auto:intel_<N>` | Intel, N jumps away. Closer = higher pitch and more pulses |
| `auto:blind` | Local view lost |
| `auto:fault` | repeated internal errors |
| `auto:tone_<Hz>_<pulses>` | custom tone, e.g. `auto:tone_1000_2` |

Local tones use a buzzy (square-like) timbre, intel tones a soft decaying sine, so they are easy to tell apart.
Higher priority sounds interrupt lower ones; interrupted and waiting sounds are replayed unless older than
`sounds.max_queue_age_sec`.

## Configuration

See `config.example.toml`; every key is documented there, and `settings` edits all of them. Unknown keys
and wrong types are reported as `config.toml:<line>: [section] key: message`.

By default only the `terrible`, `bad` and `neutral` classes signal; `good` and `excellent` are off.
You do not need to enter your UI scale, resolution or monitor layout anywhere: the region and colors are
measured from real pixels.

## Known limitations (Local watcher)

- Only rows visible in the Local window are seen: make the window tall enough or sort by standing.
- The Local window must not be covered by other windows or on a minimized client.
- Exclusive fullscreen may not be capturable; use windowed or borderless mode.
- Pilots leaving and arriving of the same color between two frames are not noticed (the count is unchanged).
- Pilot names are not recognized (no OCR, by design).
- Growth inside a class cooldown is not lost: it signals when the cooldown ends (if the icons are still there).
- Two icons of the same color with no empty row between them are counted as one.
- Without the Local chatlog the system change is not detected, and one false signal per jump is possible.

## Tests

```
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

GUI smoke tests open real windows briefly and are skipped where no display exists.
