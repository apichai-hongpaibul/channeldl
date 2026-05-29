#!/usr/bin/env python3
"""
yt-audio-dl v2  —  Concurrent channel audio downloader with live progress UI.

Architecture
────────────
  main thread          orchestrator, renders Rich Live display
  ThreadPoolExecutor   worker pool (default 5 concurrent downloads)
  DownloadTask         dataclass, owns per-video mutable state
  StateStore           thread-safe JSON persistence
  ProgressRenderer     Rich Live layout: per-task bars + summary footer

Usage
─────
  python yt_audio_dl.py @mkbhd
  python yt_audio_dl.py --list @mkbhd
  python yt_audio_dl.py --workers 3 --output ~/Music @mkbhd
  python yt_audio_dl.py --reset @mkbhd
"""

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Optional

# ── dependency checks ────────────────────────────────────────────────────────
_missing = []
try:
    import yt_dlp
except ImportError:
    _missing.append("yt-dlp")
try:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        SpinnerColumn,
        TaskID,
        TextColumn,
        TimeElapsedColumn,
        TransferSpeedColumn,
    )
    from rich.table import Table
    from rich.text import Text
    from rich import box
except ImportError:
    _missing.append("rich")

if _missing:
    sys.exit(f"❌  Missing packages: {', '.join(_missing)}\n   pip install {' '.join(_missing)}")


# ── constants ────────────────────────────────────────────────────────────────
STATE_FILE   = ".yt_audio_dl_state.json"
ARCHIVE_FILE = ".yt_audio_dl_archive.txt"
ERROR_LOG    = ".yt_audio_dl_errors.log"
DEFAULT_WORKERS = 5
DEFAULT_QUALITY = "192"

console = Console()


# ── task state machine ───────────────────────────────────────────────────────
class Status(Enum):
    QUEUED    = auto()
    FETCHING  = auto()   # resolving metadata
    ACTIVE    = auto()   # downloading + converting
    DONE      = auto()
    SKIPPED   = auto()   # already downloaded
    FAILED    = auto()


@dataclass
class DownloadTask:
    video_id:   str
    title:      str
    url:        str
    status:     Status = Status.QUEUED
    progress:   float  = 0.0    # 0.0 – 1.0
    speed:      str    = ""
    error:      str    = ""
    output_path: str   = ""
    rich_task_id: Optional["TaskID"] = field(default=None, repr=False)


# ── thread-safe state persistence ────────────────────────────────────────────
class StateStore:
    def __init__(self, output_dir: Path):
        self._path  = output_dir / STATE_FILE
        self._lock  = threading.Lock()
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"downloaded": {}}

    def is_done(self, video_id: str) -> bool:
        with self._lock:
            return video_id in self._data["downloaded"]

    def mark_done(self, task: DownloadTask) -> None:
        with self._lock:
            self._data["downloaded"][task.video_id] = {
                "title": task.title,
                "file":  task.output_path,
                "ts":    datetime.utcnow().isoformat(),
            }
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def clear(self) -> None:
        with self._lock:
            self._data = {"downloaded": {}}
            if self._path.exists():
                self._path.unlink()

    @property
    def done_count(self) -> int:
        with self._lock:
            return len(self._data["downloaded"])


# ── error logger ─────────────────────────────────────────────────────────────
def make_error_logger(output_dir: Path) -> logging.Logger:
    log = logging.getLogger("yt_audio_dl")
    log.setLevel(logging.WARNING)
    if not log.handlers:
        fh = logging.FileHandler(output_dir / ERROR_LOG, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(fh)
    return log


# ── yt-dlp helpers ───────────────────────────────────────────────────────────
def resolve_channel_url(raw: str) -> str:
    s = raw.strip()
    if s.startswith("http://") or s.startswith("https://"):
        return s
    handle = s if s.startswith("@") else f"@{s}"
    return f"https://www.youtube.com/{handle}/videos"


def sanitize(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    return name.strip(". ") or "untitled"


def fetch_video_list(channel_url: str) -> list[dict]:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)
    if not info:
        sys.exit("❌  Could not fetch channel. Check the name/URL.")
    return [
        {
            "id":    e["id"],
            "title": e.get("title") or e["id"],
            "url":   f"https://www.youtube.com/watch?v={e['id']}",
            "date":  e.get("upload_date") or "",
            "dur":   e.get("duration_string") or "?",
        }
        for e in (info.get("entries") or [])
        if e and e.get("id")
    ]


# ── single-video download worker ─────────────────────────────────────────────
def worker(
    task: DownloadTask,
    output_dir: Path,
    archive_path: Path,
    progress: Progress,
    store: StateStore,
    err_log: logging.Logger,
    quality: str = DEFAULT_QUALITY,
) -> DownloadTask:
    """Runs in a thread. Mutates task.status / task.progress in real-time."""

    task.status = Status.ACTIVE
    safe = sanitize(task.title)
    out_tmpl = str(output_dir / f"{safe}.%(ext)s")

    def _hook(d: dict) -> None:
        if d["status"] == "downloading":
            total   = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            task.speed = d.get("_speed_str", "").strip() or ""
            if total > 0:
                pct = downloaded / total
                task.progress = pct
                progress.update(
                    task.rich_task_id,
                    completed=int(pct * 100),
                    description=_row_label(task),
                )
        elif d["status"] == "finished":
            task.progress = 1.0
            progress.update(
                task.rich_task_id,
                completed=100,
                description=_row_label(task),
            )

    opts = {
        "format": "bestaudio/best",
        "outtmpl": out_tmpl,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "download_archive": str(archive_path),
        "progress_hooks": [_hook],
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality},
            {"key": "FFmpegMetadata"},
        ],
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ret = ydl.download([task.url])

        expected = output_dir / f"{safe}.mp3"
        if ret == 0 and expected.exists():
            task.status = Status.DONE
            task.output_path = str(expected)
            store.mark_done(task)
        else:
            # yt-dlp archive skip → already done on disk
            task.status = Status.SKIPPED

    except Exception as exc:
        task.status = Status.FAILED
        task.error  = str(exc)
        err_log.warning("FAILED  %s  %s  —  %s", task.video_id, task.title, exc)

    if task.status == Status.FAILED:
        err_log.warning("SKIPPED  %s  %s", task.video_id, task.title)

    # Final bar update
    icon = _status_icon(task.status)
    progress.update(
        task.rich_task_id,
        completed=100,
        description=_row_label(task, final=True),
    )
    return task


# ── Rich UI helpers ───────────────────────────────────────────────────────────
STATUS_COLORS = {
    Status.QUEUED:   "grey50",
    Status.FETCHING: "yellow",
    Status.ACTIVE:   "cyan",
    Status.DONE:     "green",
    Status.SKIPPED:  "grey62",
    Status.FAILED:   "red",
}

STATUS_ICONS = {
    Status.QUEUED:   "·",
    Status.FETCHING: "…",
    Status.ACTIVE:   "⬇",
    Status.DONE:     "✓",
    Status.SKIPPED:  "–",
    Status.FAILED:   "✗",
}

def _status_icon(s: Status) -> str:
    return STATUS_ICONS.get(s, "?")

def _row_label(task: DownloadTask, final: bool = False) -> str:
    color = STATUS_COLORS[task.status]
    icon  = _status_icon(task.status)
    title = task.title[:52] + "…" if len(task.title) > 52 else task.title
    speed = f" [{task.speed}]" if task.speed and not final else ""
    return f"[{color}]{icon}[/] {title}{speed}"


def build_progress() -> Progress:
    return Progress(
        SpinnerColumn(spinner_name="dots", style="cyan"),
        TextColumn("{task.description}", no_wrap=True),
        BarColumn(bar_width=28, style="cyan", complete_style="green", finished_style="green"),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%", style="grey62"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
        expand=False,
    )


def build_summary(tasks: list[DownloadTask], elapsed: float, total: int) -> Table:
    done    = sum(1 for t in tasks if t.status == Status.DONE)
    skipped = sum(1 for t in tasks if t.status == Status.SKIPPED)
    failed  = sum(1 for t in tasks if t.status == Status.FAILED)
    active  = sum(1 for t in tasks if t.status == Status.ACTIVE)
    queued  = sum(1 for t in tasks if t.status == Status.QUEUED)

    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True)
    grid.add_row(
        f"[green]✓ {done} done[/]",
        f"[grey62]– {skipped} skipped[/]",
        f"[cyan]⬇ {active} active[/]",
        f"[grey50]· {queued} queued[/]",
        f"[red]✗ {failed} failed[/]" if failed else "[grey30]✗ 0 failed[/]",
    )
    return grid


# ── list command ─────────────────────────────────────────────────────────────
def cmd_list(videos: list[dict], store: StateStore) -> None:
    console.print()
    for i, v in enumerate(videos, 1):
        d = v["date"]
        date = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else "    ?    "
        done = "[green]✓[/]" if store.is_done(v["id"]) else " "
        console.print(
            f"  {done} [grey50]{i:>4}.[/]  [grey62]{date}[/]  "
            f"[grey62]{v['dur']:>8}[/]  {v['title']}"
        )
    console.print()


# ── main download orchestrator ────────────────────────────────────────────────
def cmd_download(
    videos: list[dict],
    output_dir: Path,
    store: StateStore,
    err_log: logging.Logger,
    workers: int,
    quality: str = DEFAULT_QUALITY,
) -> None:
    archive_path = output_dir / ARCHIVE_FILE

    # Partition
    pending_vids = [v for v in videos if not store.is_done(v["id"])]
    already_done = len(videos) - len(pending_vids)

    if not pending_vids:
        console.print("\n[green]✓[/]  All [bold]{len(videos)}[/bold] videos already downloaded.\n")
        return

    # Build task objects
    tasks = [
        DownloadTask(video_id=v["id"], title=v["title"], url=v["url"])
        for v in pending_vids
    ]

    console.print()
    console.rule("[bold]yt-audio-dl[/]", style="grey30")
    console.print(
        f"  [grey62]channel[/]   {output_dir.name}\n"
        f"  [grey62]total[/]     {len(videos)}\n"
        f"  [grey62]pending[/]   {len(tasks)}\n"
        f"  [grey62]skipped[/]   {already_done} (already downloaded)\n"
        f"  [grey62]workers[/]   {workers}\n"
        f"  [grey62]output[/]    {output_dir.resolve()}"
    )
    console.rule(style="grey30")
    console.print()

    progress = build_progress()

    # Register all tasks in progress display (queued = 0%)
    for t in tasks:
        t.rich_task_id = progress.add_task(
            _row_label(t), total=100, completed=0
        )

    start = time.monotonic()

    with Live(progress, console=console, refresh_per_second=10):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(worker, t, output_dir, archive_path, progress, store, err_log, quality): t
                for t in tasks
            }
            for _ in as_completed(futures):
                pass   # progress updates happen inside worker via hooks

    elapsed = time.monotonic() - start

    # Final summary line
    console.print()
    console.print(build_summary(tasks, elapsed, len(videos)))
    console.print(
        f"\n  [grey50]elapsed {elapsed:.1f}s   "
        f"output {output_dir.resolve()}[/]\n"
    )

    failed = [t for t in tasks if t.status == Status.FAILED]
    if failed:
        console.print(f"  [red]✗  {len(failed)} failed — see {output_dir / ERROR_LOG}[/]\n")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        prog="yt-audio-dl",
        description="Download a YouTube channel as MP3 — concurrent, resumable.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python yt_audio_dl.py @mkbhd
  python yt_audio_dl.py --list @mkbhd
  python yt_audio_dl.py --workers 3 --output ~/Music @mkbhd
  python yt_audio_dl.py --reset @mkbhd
  python yt_audio_dl.py https://www.youtube.com/@mkbhd/videos
        """,
    )
    parser.add_argument("channel", help="Channel handle, name, or full URL")
    parser.add_argument("--list",    "-l", action="store_true", dest="list_only", help="List only, no download")
    parser.add_argument("--reset",   "-r", action="store_true", help="Clear history and re-download all")
    parser.add_argument("--workers", "-w", type=int, default=DEFAULT_WORKERS, metavar="N", help=f"Concurrent workers (default {DEFAULT_WORKERS})")
    parser.add_argument("--output",  "-o", default=None, metavar="DIR", help="Output directory")
    parser.add_argument("--quality", "-q", default=DEFAULT_QUALITY, metavar="KBPS", help=f"MP3 bitrate (default {DEFAULT_QUALITY})")

    args = parser.parse_args()

    # Output dir
    if args.output:
        output_dir = Path(args.output)
    else:
        name = args.channel.lstrip("@").split("/")[0]
        output_dir = Path(re.sub(r"[^\w\-]", "_", name))
    output_dir.mkdir(parents=True, exist_ok=True)

    store   = StateStore(output_dir)
    err_log = make_error_logger(output_dir)

    if args.reset:
        store.clear()
        archive = output_dir / ARCHIVE_FILE
        if archive.exists():
            archive.unlink()
        console.print("[yellow]↺[/]  History cleared.")

    channel_url = resolve_channel_url(args.channel)
    console.print(f"\n[grey50]Fetching video list…[/]", end="\r")
    videos = fetch_video_list(channel_url)
    console.print(f"[grey50]                       [/]", end="\r")

    if not videos:
        console.print("[yellow]⚠[/]  No videos found.")
        return

    if args.list_only:
        cmd_list(videos, store)
    else:
        cmd_download(videos, output_dir, store, err_log, args.workers, args.quality)


if __name__ == "__main__":
    main()
