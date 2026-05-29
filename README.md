# yt-audio-dl v2

Concurrent YouTube channel → MP3 downloader with live progress UI.

```
  ─────────────────────── yt-audio-dl ──────────────────────────
  channel   mkbhd
  total     312
  pending   47
  skipped   265 (already downloaded)
  workers   5
  output    /Users/you/Music/mkbhd
  ───────────────────────────────────────────────────────────────

  ⠋ ✓ First video title                      ████████████  100%  0:12
  ⠙ ⬇ Second video downloading… [1.2 MB/s]   ████████░░░░   68%  0:08
  ⠹ ⬇ Third one in progress                  ██████░░░░░░   52%  0:06
  ⠸ ⬇ Fourth one                             ████░░░░░░░░   33%  0:03
  ⠼ · Fifth queued                           ░░░░░░░░░░░░    0%  0:00

  ✓ 44 done   – 3 skipped   ⬇ 0 active   · 0 queued   ✗ 0 failed

  elapsed 4m32s   output /Users/you/Music/mkbhd
```

---

## Install

```bash
uv add -r requirements.txt

# ffmpeg required for MP3 conversion
sudo apt install ffmpeg        # Ubuntu/Debian
brew install ffmpeg            # macOS
# Windows → https://ffmpeg.org/download.html  (add to PATH)
```

---

## Usage

```bash
# Download all audio from a channel
uv run yt_audio_dl.py @mkbhd

# List videos first (shows ✓ next to already-downloaded)
uv run yt_audio_dl.py --list @mkbhd

# Custom output folder
uv run yt_audio_dl.py --output ~/Music/MKBHD @mkbhd

# Fewer workers (quieter on YouTube's rate limits)
uv run yt_audio_dl.py --workers 3 @mkbhd

# Higher quality
uv run yt_audio_dl.py --quality 320 @mkbhd

# Clear history and re-download everything
uv run yt_audio_dl.py --reset @mkbhd

# Full URL also works
uv run yt_audio_dl.py https://www.youtube.com/@mkbhd/videos
```

---

## Architecture

```
main thread
  │
  ├── fetch_video_list()       yt-dlp flat extract, no download
  ├── StateStore               thread-safe JSON state
  │
  └── ThreadPoolExecutor (N workers)
        ├── worker(task_1)  ──► yt-dlp download + ffmpeg → .mp3
        ├── worker(task_2)  ──► yt-dlp progress_hook → Rich progress bar
        ├── worker(task_3)  ──► on success → StateStore.mark_done()
        ├── worker(task_4)  ──► on failure → err_log (silent)
        └── worker(task_5)
              │
              └── Rich Live display (10 fps refresh)
                    ├── per-task progress bars
                    └── summary footer
```

### Key design decisions

| Decision | Reason |
|----------|--------|
| `ThreadPoolExecutor` not `asyncio` | yt-dlp is synchronous; threads are the correct tool |
| Two-layer state (JSON + yt-dlp archive) | JSON is human-readable / inspectable; archive is yt-dlp's own guard against re-download |
| `mark_done()` called immediately after each success | Crash-safe — lose at most one in-progress download |
| `progress_hooks` inside worker thread | yt-dlp calls hooks on the same thread; Rich's `Live` is thread-safe for updates |
| `--workers 5` default | Balanced between speed and avoiding YouTube throttling |

---

## State files (live next to MP3s)

| File | Content |
|------|---------|
| `.yt_audio_dl_state.json` | `{video_id: {title, file, ts}}` — human-readable, inspectable |
| `.yt_audio_dl_archive.txt` | yt-dlp native archive — backup re-download guard |
| `.yt_audio_dl_errors.log` | Timestamped log of skipped / failed videos |

Move the output folder anywhere — state travels with it.

---

## Flags

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--list` | `-l` | — | List videos, do not download |
| `--output DIR` | `-o` | `./<channel>` | Output directory |
| `--workers N` | `-w` | `5` | Concurrent downloads |
| `--quality KBPS` | `-q` | `192` | MP3 bitrate |
| `--reset` | `-r` | — | Clear history, re-download all |
