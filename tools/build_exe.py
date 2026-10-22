"""Builds dist/EveWatch.exe (one file, console) and dist/EveWatch-<version>-win64.zip with a quick start.

    .venv\\Scripts\\python tools/build_exe.py

Requires: pip install -r requirements.txt -r requirements-build.txt
"""

import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evewatch import __version__  # noqa: E402


def package(exe: Path, dist: Path) -> int:
    archive = dist / f"EveWatch-{__version__}-win64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(exe, "EveWatch.exe")
        z.write(ROOT / "packaging" / "QUICKSTART.txt", "QUICKSTART.txt")
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    print(f"exe: {exe} ({exe.stat().st_size / 1e6:.1f} MB)  sha256 {digest}")
    print(f"zip: {archive} ({archive.stat().st_size / 1e6:.1f} MB)")
    return 0


def main() -> int:
    dist = ROOT / "dist"
    build = ROOT / "build"
    if "--zip-only" in sys.argv:
        exe = dist / "EveWatch.exe"
        if not exe.exists():
            print("dist/EveWatch.exe does not exist; run without --zip-only first")
            return 1
        return package(exe, dist)
    for folder in (dist, build):
        shutil.rmtree(folder, ignore_errors=True)
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--console",
        "--name", "EveWatch",
        "--paths", str(ROOT),
        "--hidden-import", "PIL._tkinter_finder",
        "--hidden-import", "mss.windows",
        "--exclude-module", "pytest",
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "pandas",
        "--distpath", str(dist),
        "--workpath", str(build),
        "--specpath", str(build),
        str(ROOT / "packaging" / "EveWatch.py"),
    ]
    print(" ".join(command))
    if subprocess.call(command, cwd=ROOT) != 0:
        return 1
    exe = dist / "EveWatch.exe"
    if not exe.exists():
        print("build produced no exe")
        return 1
    return package(exe, dist)


if __name__ == "__main__":
    sys.exit(main())
