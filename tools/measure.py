"""Measures a real `run` for a while and prints CPU / RAM / threads / handles against the spec budgets.

    python tools/measure.py [--config config.toml] [--seconds 120] [--interval 5]

The run is silent (no sounds) and uses a temporary copy of the config, so it does not touch your
log file or settings. It reads the real screen region and real chatlogs, like a normal run.
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evewatch.metrics import summarize  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="evewatch-measure-") as tmp:
        tmp_dir = Path(tmp)
        source_dir = args.config.resolve().parent
        shutil.copy(args.config, tmp_dir / "config.toml")
        for name in ("universe.json", "anchor.png"):
            if (source_dir / name).exists():
                shutil.copy(source_dir / name, tmp_dir / name)
        csv_path = tmp_dir / "metrics.csv"
        command = [
            sys.executable, "-m", "evewatch", "-c", str(tmp_dir / "config.toml"),
            "run", "--silent", "--metrics", str(csv_path), "--metrics-interval", str(args.interval),
        ]
        print(f"Measuring for {args.seconds:.0f} s ...")
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        try:
            time.sleep(args.seconds)
        finally:
            process.terminate()
            process.wait(10)
        if not csv_path.exists():
            print("no metrics were written")
            return 1
        data = summarize(csv_path)
        print(f"samples: {data['samples']}")
        if not data.get("enough_data"):
            return 1
        print(f"RAM:      start {data['rss_start_mb']} MB, end {data['rss_end_mb']} MB, max {data['rss_max_mb']} MB "
              f"(budget 80 MB)")
        print(f"CPU:      average {data['cpu_avg_pct']} %, max {data['cpu_max_pct']} % of one core (budget 2 %)")
        print(f"threads:  {data['threads_min']}..{data['threads_max']}   handle growth: {data['handles_growth']:+d}   "
              f"object growth: {data['gc_objects_growth']:+d}")
        ok = data["rss_max_mb"] <= 80 and data["cpu_avg_pct"] <= 2
        print("RESULT:", "within budget" if ok else "OVER BUDGET")
        return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
