"""
Build the standalone executable.

    python build.py             one self-contained .exe (default)
    python build.py --sidecar   smaller, faster .exe with ffmpeg in a folder beside it
    python build.py --slim      .exe only, no ffmpeg at all

Packaging trade, measured on this machine
-----------------------------------------
A PyInstaller one-file build is not really a single binary: it is an archive
that unpacks itself into a temp directory on every launch and deletes it on
exit. ffmpeg's static Windows binaries are ~102 MB each, so embedding them
means writing ~205 MB to temp every time the app opens.

That cost is real but modest, so the default is the single file people actually
asked for:

    mode      exe size   cold launch
    embed       93 MB      ~3.1 s
    sidecar     21 MB      ~1.9 s

`--sidecar` writes ffmpeg once, at build time, into a folder next to the .exe;
`app.find_ffmpeg()` looks there before anywhere else. The result is a portable
folder rather than a lone file — zip it, copy it anywhere, double-click the
.exe. Worth choosing if the app is launched often, or if aggressive on-access
antivirus makes the per-launch unpack slower than the numbers above.

ffmpeg binaries come from gyan.dev, the Windows build service ffmpeg.org links
to. They are cached in `vendor/` so a rebuild does not re-download.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor"
FFMPEG_DIR = VENDOR / "ffmpeg"
DIST = ROOT / "dist"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
NEEDED = ("ffmpeg.exe", "ffprobe.exe")

APP_NAME = "YouTube Downloader"


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def fetch_ffmpeg() -> Path:
    """Download and unpack ffmpeg into vendor/ffmpeg, skipping if already there."""
    if all((FFMPEG_DIR / name).is_file() for name in NEEDED):
        log(f"ffmpeg already vendored in {FFMPEG_DIR}")
        return FFMPEG_DIR

    VENDOR.mkdir(parents=True, exist_ok=True)
    archive = VENDOR / "ffmpeg.zip"

    if not archive.is_file():
        log(f"downloading ffmpeg from {FFMPEG_URL}")
        log("(~80 MB, only on the first build)")
        with urllib.request.urlopen(FFMPEG_URL) as response, archive.open("wb") as out:
            shutil.copyfileobj(response, out)

    log("unpacking ffmpeg")
    FFMPEG_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        # The archive nests everything under a versioned folder; pull just the
        # two binaries out of its bin/ directory and flatten them.
        for member in zf.namelist():
            name = Path(member).name
            if name in NEEDED and "/bin/" in member:
                with zf.open(member) as src, (FFMPEG_DIR / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                log(f"extracted {name}")

    missing = [n for n in NEEDED if not (FFMPEG_DIR / n).is_file()]
    if missing:
        raise SystemExit(f"ffmpeg archive did not contain: {', '.join(missing)}")

    archive.unlink(missing_ok=True)
    return FFMPEG_DIR


def build(mode: str) -> tuple[Path, Path | None]:
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        APP_NAME,
        # yt-dlp pulls a lot in through lazy imports that the analyser misses.
        "--collect-submodules",
        "yt_dlp",
    ]

    if mode == "embed":
        ffmpeg_dir = fetch_ffmpeg()
        for name in NEEDED:
            # ";ffmpeg" is the folder inside the bundle, which is exactly where
            # app.find_ffmpeg() looks first. The source path must be absolute —
            # PyInstaller resolves a relative one against the .spec directory.
            args += ["--add-binary", f"{(ffmpeg_dir / name).resolve()};ffmpeg"]

    args.append(str(ROOT / "app.py"))

    log(f"running PyInstaller ({mode})")
    subprocess.run(args, cwd=ROOT, check=True)

    exe = DIST / f"{APP_NAME}.exe"
    if not exe.is_file():
        raise SystemExit("PyInstaller finished but produced no .exe")

    sidecar: Path | None = None
    if mode == "sidecar":
        ffmpeg_dir = fetch_ffmpeg()
        sidecar = DIST / "ffmpeg"
        sidecar.mkdir(parents=True, exist_ok=True)
        for name in NEEDED:
            log(f"copying {name} next to the .exe")
            shutil.copy2(ffmpeg_dir / name, sidecar / name)

    return exe, sidecar


def mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the YouTube Downloader executable.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--sidecar",
        action="store_true",
        help="ffmpeg in a folder beside the .exe: smaller and faster to start",
    )
    group.add_argument(
        "--slim",
        action="store_true",
        help="no ffmpeg at all (MP4 up to 720p, unless ffmpeg is on PATH)",
    )
    opts = parser.parse_args()

    mode = "sidecar" if opts.sidecar else "slim" if opts.slim else "embed"
    exe, sidecar = build(mode)

    print()
    print(f"Built {exe}  ({mb(exe):.0f} MB)")

    if mode == "embed":
        print()
        print("Single self-contained file. Copy it anywhere and double-click.")
        print("Verify a copy with:  \"YouTube Downloader.exe\" --selftest")
    elif sidecar:
        total = sum(mb(sidecar / n) for n in NEEDED)
        print(f"      {sidecar}\\  ({total:.0f} MB - ffmpeg, keep it beside the .exe)")
        print()
        print("Ship the whole dist folder. Double-click the .exe to run.")
    else:
        print()
        print("Slim build: MP3 and MP4 above 720p need ffmpeg on your PATH.")


if __name__ == "__main__":
    main()
