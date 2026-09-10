# YouTube Downloader

A small desktop app for saving a YouTube video as **MP4**, or just its audio as **MP3**.

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) with a tkinter front end, packaged
as a single self-contained Windows executable.

---

## Using it

Double-click `YouTube Downloader.exe`. Nothing to install.

1. Paste a video (or playlist) link.
2. Pick **MP4 — video** or **MP3 — audio**, and a quality.
3. Choose where files land — defaults to your Downloads folder.
4. Hit **Download**.

Progress, speed and time remaining show under the bar. The log pane underneath
carries anything yt-dlp has to say, which is where to look when a download fails.

**Playlists** are opt-in. Pasting a playlist link downloads only the one video you
linked unless you tick *Download every video in the playlist*.

To check an install without downloading anything:

```bat
"YouTube Downloader.exe" --selftest
```

That reports the yt-dlp version, where ffmpeg was found, and where files will be
saved.

---

## What you actually get

**MP4** downloads prefer **H.264 video + AAC audio**. YouTube's genuinely
highest-quality streams are now usually AV1 or VP9 paired with Opus — a smaller
file that is still a valid `.mp4`, but that Windows' built-in player, most TVs
and most video editors refuse to open. Asking for H.264 first means the file
plays everywhere. Above 1080p, where YouTube offers nothing else, it falls back
to the newer codecs automatically.

**MP3** is a real transcode to MP3 at your chosen bitrate, not a renamed audio
stream.

---

## Building it yourself

Requires Python 3.10+ on Windows.

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python build.py
```

The executable lands in `dist\YouTube Downloader.exe`.

| Command | Result |
| --- | --- |
| `python build.py` | One self-contained `.exe`, ffmpeg inside. **Default.** |
| `python build.py --sidecar` | Smaller, faster `.exe` with ffmpeg in a folder beside it. |
| `python build.py --slim` | `.exe` only. MP4 up to 720p, unless ffmpeg is on your PATH. |

`build.py` downloads ffmpeg from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/)
(the Windows build service ffmpeg.org links to) and caches it in `vendor/`, so
only the first build pays for it.

### The packaging trade

A PyInstaller one-file build is not really a single binary: it is an archive
that unpacks itself into a temp directory on every launch and deletes it on
exit. ffmpeg's static Windows binaries are ~102 MB each, so the default build
writes ~205 MB to temp each time it opens. Measured here:

| Mode | Exe size | Cold launch |
| --- | --- | --- |
| default (embedded) | 92 MB | ~3.1 s |
| `--sidecar` | 21 MB | ~1.9 s |

The default is the single file, because that is usually what "just give me an
app" means. Use `--sidecar` if you launch it often, or if aggressive on-access
antivirus makes the per-launch unpack slower than the numbers above — it writes
ffmpeg once, at build time, and the app looks for it next to the `.exe`.

### Why ffmpeg is needed at all

Above 720p, YouTube serves video and audio as **separate streams**, so producing
one MP4 means merging them. MP3 means transcoding an audio stream that arrives
as webm or m4a. Both are ffmpeg's job. Without it the app is limited to the
progressive MP4 renditions that stop at 720p.

### Running from source

```bat
.venv\Scripts\python app.py
```

ffmpeg needs to be on your PATH, or in an `ffmpeg\` folder next to `app.py`.

---

## Keeping it working

YouTube changes how it serves video fairly often, and yt-dlp tracks those
changes closely. When downloads start failing for no obvious reason, update it
and rebuild:

```bat
.venv\Scripts\pip install --upgrade yt-dlp
.venv\Scripts\python build.py
```

That fixes the large majority of "it stopped working" problems.

---

## Layout

```
app.py         the GUI, the download logic, and --selftest
build.py       fetches ffmpeg, runs PyInstaller
vendor/        cached ffmpeg binaries (not in git)
dist/          the built executable (not in git)
```

---

## A note on what you download

This is a tool, and what it is pointed at is your call. Downloading your own
uploads, Creative Commons material, or anything you have permission to keep is
uncontroversial; redistributing other people's work generally is not. Bulk
downloading also runs against YouTube's terms of service.
