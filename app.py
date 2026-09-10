"""
YouTube Downloader — a small tkinter front end for yt-dlp.

Two jobs, and the rest of the design follows from them: fetch a video as MP4,
or its audio as MP3. Quality, destination and playlist handling are switches on
top of those two paths.

Threading model
---------------
yt-dlp blocks, so it runs on a worker thread and never touches a widget.
Progress arrives as plain dicts on a queue.Queue which the Tk main loop drains
on a timer. That keeps tkinter's "every widget call happens on the main thread"
rule intact without a single lock, and it is why cancellation is an Event the
worker polls rather than anything that reaches into the UI.

ffmpeg
------
MP3 extraction and any MP4 above 720p both need ffmpeg: YouTube serves high
quality video and audio as separate streams, so there is always a merge or a
transcode at the end. `build.py` bundles ffmpeg next to the app, and
`find_ffmpeg()` falls back to a copy on PATH for a plain source run.
"""

from __future__ import annotations

import queue
import shutil
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import time as _time

_STARTED = _time.time()

APP_NAME = "YouTube Downloader"
POLL_MS = 80

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
# ttk's native Windows themes ignore most colour options, so 'clam' is the
# starting point — it is the one built-in theme that honours a full restyle.
BG = "#0f1420"
PANEL = "#161d2c"
FIELD = "#1b2434"
LINE = "#2a3446"
TEXT = "#e8eef6"
MUTED = "#93a1b5"
ACCENT = "#00d8ef"
ACCENT_DIM = "#0aa8bd"
DANGER = "#ff6b6b"
OK = "#4ade80"

VIDEO_QUALITIES: dict[str, int | None] = {
    "Best available": None,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}

AUDIO_QUALITIES: dict[str, str] = {
    "320 kbps": "320",
    "192 kbps": "192",
    "128 kbps": "128",
}


class DownloadCancelled(Exception):
    """Raised inside yt-dlp's progress hook to unwind a running download."""


def _resource_dir() -> Path:
    """Where bundled data lives: PyInstaller's temp unpack dir, or the source tree."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parent


def find_ffmpeg() -> Path | None:
    """
    Locate a usable ffmpeg, preferring the bundled copy.

    Returns the *directory* holding the binary, which is what yt-dlp's
    `ffmpeg_location` wants, or None when there is nothing to find.
    """
    roots = [
        _resource_dir() / "ffmpeg",
        _resource_dir(),
        Path(sys.executable).resolve().parent / "ffmpeg",
        Path(sys.executable).resolve().parent,
    ]
    for root in roots:
        for name in ("ffmpeg.exe", "ffmpeg"):
            if (root / name).is_file():
                return root

    found = shutil.which("ffmpeg")
    return Path(found).resolve().parent if found else None


def default_download_dir() -> Path:
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


def build_options(
    *,
    audio_only: bool,
    quality: str,
    out_dir: Path,
    playlist: bool,
    ffmpeg_dir: Path | None,
    hook,
    pp_hook,
    logger,
) -> dict:
    """Translate the form into a yt-dlp options dict."""
    opts: dict = {
        "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
        "noplaylist": not playlist,
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
        "logger": logger,
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        # Keep one partial file per download rather than resuming a stale one.
        "continuedl": True,
        "retries": 5,
        "fragment_retries": 5,
        # Windows filenames reject characters that video titles are full of.
        "windowsfilenames": True,
        "restrictfilenames": False,
    }

    if ffmpeg_dir is not None:
        opts["ffmpeg_location"] = str(ffmpeg_dir)

    if audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": AUDIO_QUALITIES.get(quality, "192"),
            }
        ]
        return opts

    height = VIDEO_QUALITIES.get(quality)
    # `<=?` keeps the cap advisory: a video with no rendition at or below it
    # still downloads at whatever it does have, instead of failing outright.
    cap = "" if height is None else f"[height<=?{height}]"

    # Preference order matters more than it looks. YouTube's genuinely "best"
    # streams are now usually AV1 or VP9 paired with Opus — a smaller file that
    # is a valid MP4 and that Windows' built-in player, most TVs and most video
    # editors will not open. Someone who picked "MP4" wants a file that plays,
    # so ask for H.264 + AAC first and only fall back to the newer codecs when
    # YouTube offers nothing else (which is what happens above 1080p).
    opts["format"] = (
        f"bestvideo{cap}[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
        f"bestvideo{cap}[ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo{cap}+bestaudio/"
        f"best{cap}/best"
    )

    opts["merge_output_format"] = "mp4"
    # The merge above settles the container only when the streams are already
    # mp4-compatible; the remuxer covers a webm-only best rendition.
    opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    return opts


class QueueLogger:
    """Adapts yt-dlp's logger protocol onto the UI queue."""

    def __init__(self, q: queue.Queue):
        self.q = q

    def debug(self, msg: str) -> None:
        # yt-dlp routes ordinary messages through debug(); the '[' prefix marks
        # the ones worth showing (e.g. '[download] Destination: ...').
        if msg.startswith("[") and "Deleting original file" not in msg:
            self.q.put({"type": "log", "text": msg})

    def info(self, msg: str) -> None:
        self.q.put({"type": "log", "text": msg})

    def warning(self, msg: str) -> None:
        self.q.put({"type": "log", "text": f"Warning: {msg}"})

    def error(self, msg: str) -> None:
        self.q.put({"type": "log", "text": f"Error: {msg}", "tag": "error"})


def _human(size: float | None) -> str:
    if not size:
        return "?"
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def run_download(
    url: str,
    *,
    audio_only: bool,
    quality: str,
    out_dir: Path,
    playlist: bool,
    q: queue.Queue,
    cancel: threading.Event,
) -> None:
    """Worker body. Everything it reports goes onto `q`; it touches no widgets."""
    from yt_dlp import YoutubeDL  # imported here so the window paints immediately

    def hook(d: dict) -> None:
        if cancel.is_set():
            raise DownloadCancelled()

        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            speed = d.get("speed")
            eta = d.get("eta")
            parts = [f"{_human(done)} of {_human(total)}"]
            if speed:
                parts.append(f"{_human(speed)}/s")
            if eta:
                parts.append(f"{int(eta)}s left")
            q.put(
                {
                    "type": "progress",
                    "fraction": (done / total) if total else None,
                    "text": "  ·  ".join(parts),
                }
            )
        elif d.get("status") == "finished":
            q.put({"type": "progress", "fraction": 1.0, "text": "Processing…"})

    def pp_hook(d: dict) -> None:
        if cancel.is_set():
            raise DownloadCancelled()
        if d.get("status") == "started":
            name = d.get("postprocessor", "")
            label = {
                "FFmpegExtractAudio": "Converting to MP3…",
                "FFmpegVideoRemuxer": "Packaging as MP4…",
                "FFmpegMerger": "Merging video and audio…",
            }.get(name)
            if label:
                q.put({"type": "status", "text": label})

    ffmpeg_dir = find_ffmpeg()
    opts = build_options(
        audio_only=audio_only,
        quality=quality,
        out_dir=out_dir,
        playlist=playlist,
        ffmpeg_dir=ffmpeg_dir,
        hook=hook,
        pp_hook=pp_hook,
        logger=QueueLogger(q),
    )

    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
    except DownloadCancelled:
        q.put({"type": "done", "ok": False, "message": "Cancelled."})
        return
    except Exception as exc:  # noqa: BLE001 — surfaced to the user verbatim
        q.put({"type": "done", "ok": False, "message": str(exc) or exc.__class__.__name__})
        return

    q.put({"type": "done", "ok": True, "message": f"Saved to {out_dir}"})


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.configure(bg=BG)
        self.minsize(640, 520)
        self.geometry("720x600")

        self.queue: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None

        self.url_var = tk.StringVar()
        self.kind_var = tk.StringVar(value="mp4")
        self.quality_var = tk.StringVar(value="Best available")
        self.dir_var = tk.StringVar(value=str(default_download_dir()))
        self.playlist_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Paste a YouTube link to start.")

        self._build_style()
        self._build_ui()
        self._check_ffmpeg()
        self.after(POLL_MS, self._drain)

    # -- chrome ------------------------------------------------------------
    def _build_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(".", background=BG, foreground=TEXT, borderwidth=0)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure(
            "Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 16)
        )
        style.configure(
            "Status.TLabel", background=BG, foreground=MUTED, font=("Consolas", 9)
        )

        style.configure(
            "TRadiobutton", background=BG, foreground=TEXT, font=("Segoe UI", 10)
        )
        style.map(
            "TRadiobutton",
            background=[("active", BG)],
            foreground=[("active", ACCENT)],
        )
        style.configure(
            "TCheckbutton", background=BG, foreground=MUTED, font=("Segoe UI", 9)
        )
        style.map(
            "TCheckbutton",
            background=[("active", BG)],
            foreground=[("active", ACCENT)],
        )

        style.configure(
            "TEntry",
            fieldbackground=FIELD,
            foreground=TEXT,
            insertcolor=ACCENT,
            bordercolor=LINE,
            lightcolor=LINE,
            darkcolor=LINE,
            padding=8,
        )
        style.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])

        style.configure(
            "TCombobox",
            fieldbackground=FIELD,
            background=FIELD,
            foreground=TEXT,
            arrowcolor=ACCENT,
            bordercolor=LINE,
            lightcolor=LINE,
            darkcolor=LINE,
            padding=6,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", FIELD)],
            bordercolor=[("focus", ACCENT)],
        )

        style.configure(
            "Accent.TButton",
            background=ACCENT,
            foreground="#06121a",
            font=("Segoe UI Semibold", 10),
            padding=(18, 9),
            borderwidth=0,
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_DIM), ("disabled", LINE)],
            foreground=[("disabled", MUTED)],
        )

        style.configure(
            "Ghost.TButton",
            background=PANEL,
            foreground=TEXT,
            font=("Segoe UI", 10),
            padding=(14, 9),
            borderwidth=0,
        )
        style.map(
            "Ghost.TButton",
            background=[("active", LINE), ("disabled", BG)],
            foreground=[("disabled", LINE)],
        )

        style.configure(
            "TProgressbar",
            background=ACCENT,
            troughcolor=FIELD,
            bordercolor=FIELD,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=6,
        )

    def _build_ui(self) -> None:
        pad = {"padx": 22}
        root = ttk.Frame(self, style="TFrame")
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)

        ttk.Label(root, text=APP_NAME, style="Title.TLabel").grid(
            row=0, column=0, sticky="w", pady=(20, 2), **pad
        )
        ttk.Label(
            root, text="Save a video as MP4, or just its audio as MP3.", style="Muted.TLabel"
        ).grid(row=1, column=0, sticky="w", pady=(0, 18), **pad)

        # -- URL
        ttk.Label(root, text="Video or playlist link").grid(
            row=2, column=0, sticky="w", pady=(0, 6), **pad
        )
        url_entry = ttk.Entry(root, textvariable=self.url_var, font=("Segoe UI", 10))
        url_entry.grid(row=3, column=0, sticky="ew", **pad)
        url_entry.focus_set()

        # -- format + quality
        opts = ttk.Frame(root, style="TFrame")
        opts.grid(row=4, column=0, sticky="ew", pady=(16, 0), **pad)

        ttk.Radiobutton(
            opts, text="MP4 — video", value="mp4",
            variable=self.kind_var, command=self._on_kind_change,
        ).pack(side="left")
        ttk.Radiobutton(
            opts, text="MP3 — audio", value="mp3",
            variable=self.kind_var, command=self._on_kind_change,
        ).pack(side="left", padx=(18, 0))

        self.quality_box = ttk.Combobox(
            opts,
            textvariable=self.quality_var,
            values=list(VIDEO_QUALITIES),
            state="readonly",
            width=16,
            font=("Segoe UI", 10),
        )
        self.quality_box.pack(side="right")
        ttk.Label(opts, text="Quality", style="Muted.TLabel").pack(side="right", padx=(0, 8))

        ttk.Checkbutton(
            root,
            text="Download every video in the playlist",
            variable=self.playlist_var,
        ).grid(row=5, column=0, sticky="w", pady=(12, 0), **pad)

        # -- destination
        ttk.Label(root, text="Save to").grid(row=6, column=0, sticky="w", pady=(18, 6), **pad)
        dest = ttk.Frame(root, style="TFrame")
        dest.grid(row=7, column=0, sticky="ew", **pad)
        dest.columnconfigure(0, weight=1)
        ttk.Entry(dest, textvariable=self.dir_var, font=("Segoe UI", 10)).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(dest, text="Browse", style="Ghost.TButton", command=self._pick_dir).grid(
            row=0, column=1, padx=(8, 0)
        )

        # -- actions
        actions = ttk.Frame(root, style="TFrame")
        actions.grid(row=8, column=0, sticky="ew", pady=(20, 0), **pad)
        self.download_btn = ttk.Button(
            actions, text="Download", style="Accent.TButton", command=self._start
        )
        self.download_btn.pack(side="left")
        self.cancel_btn = ttk.Button(
            actions, text="Cancel", style="Ghost.TButton",
            command=self._cancel, state="disabled",
        )
        self.cancel_btn.pack(side="left", padx=(10, 0))
        ttk.Button(
            actions, text="Open folder", style="Ghost.TButton", command=self._open_dir
        ).pack(side="right")

        # -- progress
        self.progress = ttk.Progressbar(root, mode="determinate", maximum=1000)
        self.progress.grid(row=9, column=0, sticky="ew", pady=(20, 6), **pad)
        ttk.Label(root, textvariable=self.status_var, style="Status.TLabel").grid(
            row=10, column=0, sticky="w", **pad
        )

        # -- log
        root.rowconfigure(11, weight=1)
        log_wrap = tk.Frame(root, bg=LINE, highlightthickness=0, bd=0)
        log_wrap.grid(row=11, column=0, sticky="nsew", pady=(14, 20), **pad)
        self.log = tk.Text(
            log_wrap,
            bg=PANEL,
            fg=MUTED,
            insertbackground=ACCENT,
            font=("Consolas", 9),
            bd=0,
            padx=12,
            pady=10,
            wrap="word",
            state="disabled",
            height=8,
        )
        self.log.pack(fill="both", expand=True, padx=1, pady=1)
        self.log.tag_configure("error", foreground=DANGER)
        self.log.tag_configure("ok", foreground=OK)

    # -- helpers -----------------------------------------------------------
    def _check_ffmpeg(self) -> None:
        if find_ffmpeg() is None:
            self._log(
                "ffmpeg was not found. MP3 conversion and MP4 above 720p both need it — "
                "see the README for how to add it.",
                "error",
            )

    def _on_kind_change(self) -> None:
        audio = self.kind_var.get() == "mp3"
        values = list(AUDIO_QUALITIES if audio else VIDEO_QUALITIES)
        self.quality_box.configure(values=values)
        self.quality_var.set("192 kbps" if audio else "Best available")

    def _pick_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.dir_var.get() or str(Path.home()))
        if chosen:
            self.dir_var.set(chosen)

    def _open_dir(self) -> None:
        target = Path(self.dir_var.get())
        if not target.is_dir():
            messagebox.showwarning(APP_NAME, "That folder does not exist yet.")
            return
        if sys.platform == "win32":
            import os

            os.startfile(target)  # noqa: S606 — a user-chosen directory
        else:
            import subprocess

            subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", str(target)])

    def _log(self, text: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n", tag or "")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        self.download_btn.configure(state="disabled" if running else "normal")
        self.cancel_btn.configure(state="normal" if running else "disabled")

    # -- actions -----------------------------------------------------------
    def _start(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo(APP_NAME, "Paste a YouTube link first.")
            return

        out_dir = Path(self.dir_var.get().strip() or default_download_dir())
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Cannot use that folder:\n{exc}")
            return

        audio_only = self.kind_var.get() == "mp3"
        if audio_only and find_ffmpeg() is None:
            messagebox.showerror(
                APP_NAME,
                "MP3 needs ffmpeg, which is not installed.\n\n"
                "See the README for how to add it, or download as MP4 instead.",
            )
            return

        self.cancel.clear()
        self.progress.configure(value=0)
        self.status_var.set("Starting…")
        self._set_running(True)
        self._log(f"→ {url}")

        self.worker = threading.Thread(
            target=run_download,
            args=(url,),
            kwargs={
                "audio_only": audio_only,
                "quality": self.quality_var.get(),
                "out_dir": out_dir,
                "playlist": self.playlist_var.get(),
                "q": self.queue,
                "cancel": self.cancel,
            },
            daemon=True,
        )
        self.worker.start()

    def _cancel(self) -> None:
        self.cancel.set()
        self.status_var.set("Cancelling…")
        self.cancel_btn.configure(state="disabled")

    # -- the pump ----------------------------------------------------------
    def _drain(self) -> None:
        """Move whatever the worker reported into the UI. Main thread only."""
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg.get("type")

                if kind == "progress":
                    fraction = msg.get("fraction")
                    if fraction is None:
                        self.progress.configure(mode="indeterminate")
                        self.progress.start(12)
                    else:
                        self.progress.stop()
                        self.progress.configure(mode="determinate", value=fraction * 1000)
                    self.status_var.set(msg.get("text", ""))
                elif kind == "status":
                    self.status_var.set(msg["text"])
                elif kind == "log":
                    self._log(msg["text"], msg.get("tag"))
                elif kind == "done":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    ok = msg.get("ok", False)
                    self.progress.configure(value=1000 if ok else 0)
                    self.status_var.set(msg.get("message", ""))
                    self._log(("✓ " if ok else "✕ ") + msg.get("message", ""), "ok" if ok else "error")
                    self._set_running(False)
        except queue.Empty:
            pass

        self.after(POLL_MS, self._drain)


def selftest() -> int:
    """
    Report what the app can see, then exit.

    Useful two ways: it tells a user whether their copy found ffmpeg without
    making them start a download to find out, and because it exits instead of
    entering the main loop, it is also how the build measures real startup cost
    (a one-file executable unpacks itself before any of this runs).
    """
    import time

    print(f"{APP_NAME} - self test")
    print(f"  python      : {sys.version.split()[0]}")
    print(f"  frozen      : {getattr(sys, 'frozen', False)}")

    try:
        from yt_dlp.version import __version__ as ytdlp_version

        print(f"  yt-dlp      : {ytdlp_version}")
    except Exception as exc:  # noqa: BLE001
        print(f"  yt-dlp      : FAILED to import - {exc}")
        return 1

    ffmpeg_dir = find_ffmpeg()
    if ffmpeg_dir is None:
        print("  ffmpeg      : not found - MP3 and MP4 above 720p unavailable")
    else:
        print(f"  ffmpeg      : {ffmpeg_dir}")

    print(f"  saves to    : {default_download_dir()}")
    print(f"  startup     : {time.time() - _STARTED:.2f}s to here")
    return 0


def _selftest_report() -> str:
    """The same facts as selftest(), gathered as text for a windowed build."""
    import time

    lines = [f"Python {sys.version.split()[0]}", f"Frozen: {getattr(sys, 'frozen', False)}"]
    try:
        from yt_dlp.version import __version__ as ytdlp_version

        lines.append(f"yt-dlp {ytdlp_version}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"yt-dlp FAILED to import: {exc}")

    ffmpeg_dir = find_ffmpeg()
    lines.append(
        f"ffmpeg: {ffmpeg_dir}" if ffmpeg_dir
        else "ffmpeg: NOT FOUND - MP3 and MP4 above 720p unavailable"
    )
    lines.append(f"Saves to: {default_download_dir()}")
    lines.append(f"Started in {time.time() - _STARTED:.2f}s")
    return "\n".join(lines)


def main() -> None:
    # A windowed PyInstaller build has no console, so stdout/stderr are None and
    # any stray write from a dependency would raise. Give them somewhere to go.
    has_console = sys.stdout is not None and sys.stderr is not None
    if not has_console:
        import io

        sys.stdout = sys.stdout or io.StringIO()
        sys.stderr = sys.stderr or io.StringIO()

    if "--selftest" in sys.argv[1:]:
        # A windowed build's stdout is the throwaway buffer stubbed in above, so
        # printing there would go nowhere a user could see. Show a dialog instead.
        if has_console:
            raise SystemExit(selftest())
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(f"{APP_NAME} - self test", _selftest_report())
        root.destroy()
        raise SystemExit(0)

    App().mainloop()


if __name__ == "__main__":
    main()
