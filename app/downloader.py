"""Playlist resolution and downloading for YouTube / YouTube Music and Spotify.

YouTube is driven through yt-dlp's Python API one track at a time, which keeps
per-track progress accurate and stops a single dead video from killing the run.
Spotify goes through spotDL as a subprocess: it reads the playlist metadata from
the Spotify API and sources the audio from YouTube Music. Subprocess rather than
the Python API because spotDL spins up its own event loop and process pool,
which does not mix well with running inside a worker thread.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import yt_dlp

from . import config

# --------------------------------------------------------------------------- #
# URL handling
# --------------------------------------------------------------------------- #

_YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be", "www.youtu.be",
}
_SPOTIFY_HOSTS = {"open.spotify.com", "spotify.com", "www.spotify.com", "play.spotify.com"}


class UnsupportedURL(ValueError):
    pass


def detect_source(url: str) -> str:
    """Return 'youtube' or 'spotify' for a supported URL, else raise."""
    url = url.strip()
    if not url:
        raise UnsupportedURL("Keine URL angegeben.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower()
    if host in _YOUTUBE_HOSTS:
        return "youtube"
    if host in _SPOTIFY_HOSTS:
        return "spotify"
    raise UnsupportedURL(
        "Nur YouTube-, YouTube-Music- und Spotify-Links werden unterstützt."
    )


def normalise_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str, fallback: str = "playlist", limit: int = 90) -> str:
    """Filesystem- and Content-Disposition-safe version of a playlist title."""
    cleaned = _UNSAFE.sub("_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    if not cleaned:
        return fallback
    return cleaned[:limit].strip(" ._") or fallback


# --------------------------------------------------------------------------- #
# YouTube / YouTube Music
# --------------------------------------------------------------------------- #

def _base_ydl_opts() -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "consoletitle": False,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "ignoreerrors": True,
        "noplaylist": False,
    }
    if config.FORCE_IPV4:
        # Binding the source address to the v4 wildcard forces an IPv4 route;
        # YouTube blocks datacentre IPv6 far more aggressively.
        opts["source_address"] = "0.0.0.0"
    if config.COOKIES_FILE.is_file():
        opts["cookiefile"] = str(config.COOKIES_FILE)
    return opts


def resolve_youtube(url: str) -> tuple[str, list[dict]]:
    """Return (playlist_title, [{id, url, title, duration}, ...]) without downloading."""
    opts = _base_ydl_opts() | {
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        raise RuntimeError("Playlist konnte nicht gelesen werden (privat oder gelöscht?).")

    if info.get("_type") in {"playlist", "multi_video"} or "entries" in info:
        title = info.get("title") or "playlist"
        tracks = []
        for entry in info.get("entries") or []:
            if not entry:
                continue  # unavailable / private entry
            if entry.get("_type") in {"playlist", "multi_video"}:
                continue  # nested playlist (e.g. a channel), skip
            video_id = entry.get("id")
            tracks.append({
                "id": video_id,
                "url": entry.get("url") or (
                    f"https://www.youtube.com/watch?v={video_id}" if video_id else None
                ),
                "title": entry.get("title") or video_id or "Unbekannt",
                "duration": entry.get("duration"),
            })
        tracks = [t for t in tracks if t["url"]]
        return title, tracks

    # A single video / track.
    return (info.get("title") or "track"), [{
        "id": info.get("id"),
        "url": info.get("webpage_url") or url,
        "title": info.get("title") or "Unbekannt",
        "duration": info.get("duration"),
    }]


def _postprocessors(fmt: dict) -> list[dict]:
    pps: list[dict] = [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": fmt["codec"],
        "preferredquality": fmt["quality"],
    }]
    pps.append({"key": "FFmpegMetadata", "add_metadata": True})
    pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    return pps


def download_youtube_track(
    track: dict,
    index: int,
    dest: Path,
    fmt: dict,
    on_progress: Callable[[float], None],
) -> None:
    """Download one track into `dest`. Raises on failure."""
    # `%(title).120B` truncates the *title field* to 120 bytes. Do not use
    # yt-dlp's `trim_file_name`: it slices the whole absolute path, not the name.
    outtmpl = str(dest / f"{index:03d} - %(title).120B.%(ext)s")

    def hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        done = d.get("downloaded_bytes") or 0
        if total:
            on_progress(min(done / total, 1.0))

    opts = _base_ydl_opts() | {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "writethumbnail": True,
        "postprocessors": _postprocessors(fmt),
        "progress_hooks": [hook],
        "ignoreerrors": False,   # per-track: we want the exception
        "noplaylist": True,
        "concurrent_fragment_downloads": 4,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([track["url"]])


# --------------------------------------------------------------------------- #
# Spotify (via spotDL)
# --------------------------------------------------------------------------- #

_SPOTDL_FORMATS = {"mp3", "flac", "ogg", "opus", "m4a", "wav"}
_DOWNLOADED_RE = re.compile(r'^\s*Downloaded\s+"(?P<name>.+)"', re.I)


def _spotdl_base_cmd() -> list[str]:
    exe = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "spotdl"
    cmd = [str(exe) if exe.is_file() else "spotdl"]
    if config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET:
        cmd += ["--client-id", config.SPOTIFY_CLIENT_ID,
                "--client-secret", config.SPOTIFY_CLIENT_SECRET]
    if config.COOKIES_FILE.is_file():
        cmd += ["--cookie-file", str(config.COOKIES_FILE)]
    return cmd


def spotify_number_field(url: str) -> str:
    """Which spotDL template field orders the files, if any.

    `{list-position}` is only populated for playlists; albums carry
    `{track-number}`; a bare track has neither and must not get a prefix, or
    every filename starts with a stray " - ".
    """
    path = urlparse(url).path.lower()
    if "/playlist/" in path:
        return "{list-position}"
    if "/album/" in path:
        return "{track-number}"
    return ""


def resolve_spotify(url: str, workdir: Path) -> tuple[str, list[dict], Path]:
    """Fetch the track list via `spotdl save`.

    Returns (title, tracks, save_file). The save file is reused for the download
    so the Spotify API is only queried once.
    """
    save_file = workdir / "tracks.spotdl"
    cmd = _spotdl_base_cmd() + [
        "save", url,
        "--save-file", str(save_file),
        "--log-level", "ERROR",
        "--simple-tui",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=600, cwd=str(workdir)
    )
    if not save_file.is_file():
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = " ".join(detail[-3:]) if detail else "keine Ausgabe"
        raise RuntimeError(f"Spotify-Playlist konnte nicht gelesen werden: {tail}")

    data = json.loads(save_file.read_text(encoding="utf-8"))
    tracks = []
    for song in data:
        artists = song.get("artists") or []
        artist = ", ".join(artists) if artists else (song.get("artist") or "")
        name = song.get("name") or "Unbekannt"
        tracks.append({
            "title": f"{artist} – {name}" if artist else name,
            "duration": song.get("duration"),
        })

    title = ""
    if data:
        title = data[0].get("list_name") or data[0].get("album_name") or ""
    if not title:
        title = _spotify_title_from_url(url)
    return title, tracks, save_file


def _spotify_title_from_url(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) >= 2:
        return f"spotify-{parts[-2]}-{parts[-1][:12]}"
    return "spotify-playlist"


def download_spotify(
    save_file: Path,
    dest: Path,
    fmt: dict,
    total: int,
    number_field: str,
    on_track_done: Callable[[int, str], None],
    on_line: Callable[[str], None],
    on_start: Callable[[subprocess.Popen], None] | None = None,
) -> list[str]:
    """Run `spotdl download` and report progress line by line.

    Returns the list of track names spotDL reported as failed.
    """
    codec = fmt["codec"] if fmt["codec"] in _SPOTDL_FORMATS else "mp3"
    bitrate = f'{fmt["quality"]}k' if fmt["codec"] == "mp3" else "auto"

    cmd = _spotdl_base_cmd() + [
        "download", str(save_file),
        "--output", str(dest / f"{number_field}{' - ' if number_field else ''}"
                        "{artists} - {title}.{output-ext}"),
        "--format", codec,
        "--bitrate", bitrate,
        "--threads", "3",
        "--max-retries", "3",
        "--overwrite", "skip",
        "--print-errors",
        "--simple-tui",
        "--log-level", "INFO",
        "--max-filename-length", "120",
    ]

    done = 0
    failures: list[str] = []
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(dest.parent),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    if on_start is not None:
        on_start(proc)
    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            on_line(line)
            m = _DOWNLOADED_RE.match(line)
            if m:
                done += 1
                on_track_done(min(done, total), m.group("name"))
            elif "LookupError" in line or "AudioProviderError" in line:
                failures.append(line.strip()[:200])
    finally:
        proc.stdout.close()
        rc = proc.wait()

    if rc != 0 and done == 0:
        raise RuntimeError("spotDL-Download fehlgeschlagen (keine Titel geladen).")
    return failures


# --------------------------------------------------------------------------- #
# Packaging
# --------------------------------------------------------------------------- #

_AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".aac", ".webm"}


def collect_audio_files(dest: Path) -> list[Path]:
    return sorted(
        p for p in dest.rglob("*")
        if p.is_file() and p.suffix.lower() in _AUDIO_EXTS
    )


def make_zip(files: list[Path], root: Path, zip_path: Path) -> int:
    """Zip `files` with no compression (audio is already compressed) and return the size."""
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for f in files:
            try:
                arcname = f.relative_to(root)
            except ValueError:
                arcname = Path(f.name)
            zf.write(f, arcname.as_posix())
    return zip_path.stat().st_size


def cleanup_partials(dest: Path) -> None:
    """Remove leftovers yt-dlp/spotDL may leave behind."""
    for pattern in ("*.part", "*.ytdl", "*.temp", "*.webp", "*.jpg", "*.png"):
        for p in dest.rglob(pattern):
            try:
                p.unlink()
            except OSError:
                pass
    for p in dest.rglob("*"):
        if p.is_dir() and not any(p.iterdir()):
            shutil.rmtree(p, ignore_errors=True)
