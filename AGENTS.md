# channeldl (yt-audio-dl v2)

Concurrent YouTube channel → MP3 downloader with live progress UI.

## Key files

- `yt_audio_dl.py` — single-file CLI app, all logic here
- `README.md` — usage, flags, architecture overview

## Dependencies

- `yt-dlp` — YouTube extraction + download
- `rich` — terminal live display + progress bars
- `ffmpeg` — MP3 conversion (system binary)

## Run

```bash
uv run yt_audio_dl.py @channelname
```

Setup first: `uv add -r requirements.txt`

Flags: `--list`, `--output DIR`, `--workers N`, `--quality KBPS`, `--reset`.

## State files (generated in output directory)

- `.yt_audio_dl_state.json` — download history (human-readable)
- `.yt_audio_dl_archive.txt` — yt-dlp native archive
- `.yt_audio_dl_errors.log` — skipped/failed log

## Coding conventions

- Single-file Python script (no split unless project grows significantly)
- f-strings over `%` or `.format()`
- Threading: `ThreadPoolExecutor` (yt-dlp is synchronous)
- Rich `Live` for real-time UI updates
- `argparse` for CLI, no external CLI framework
- Type hints on function signatures
- No classes unless complexity warrants it; compose with functions
- `mark_done()` immediately after download success (crash safety)
- No external config files; all config via CLI flags
