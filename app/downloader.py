"""Playlist resolution and downloading for YouTube / YouTube Music and Spotify.

YouTube is driven through yt-dlp's Python API with a bounded pool of tracks,
keeping per-track failures separate so a dead video cannot kill the run.
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
import time
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


def _extract_flat(url: str) -> dict:
    opts = _base_ydl_opts() | {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "playlistend": config.MAX_TRACKS + 1,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise RuntimeError("Playlist konnte nicht gelesen werden (privat oder gelöscht?).")
    return info


def resolve_youtube(url: str) -> tuple[str, list[dict], bool]:
    """Return title, tracks and collection status; retain single-video metadata."""
    info: dict = {}
    entries: list = []
    is_collection = False
    for attempt in range(1, config.RESOLVE_RETRIES + 1):
        info = _extract_flat(url)
        is_collection = info.get("_type") in {"playlist", "multi_video"} or "entries" in info
        if not is_collection:
            break
        entries = list(info.get("entries") or [])
        if not any(e is None for e in entries):
            break
        # yt-dlp's own `ignoreerrors` (needed so one unavailable/private
        # video doesn't abort the whole listing) also swallows a transient
        # page-fetch error while paginating a large channel/playlist (a 403
        # from YouTube's browse API, seen live) — turning it into a missing
        # entry instead of raising, and silently truncating everything after
        # it. Re-extracting the whole listing from scratch after a short
        # backoff is what actually recovers the rest; these blocks have been
        # observed to clear within seconds rather than persist.
        if attempt < config.RESOLVE_RETRIES:
            time.sleep(config.RESOLVE_RETRY_DELAY * attempt)

    if not is_collection:
        # A single video / track.
        return (info.get("title") or "track"), [{
            "id": info.get("id"),
            "url": info.get("webpage_url") or url,
            "title": info.get("title") or "Unbekannt",
            "duration": info.get("duration"),
            "_info": info,
        }], False

    # A bare channel URL (no /videos, /shorts, …) does not resolve to its
    # uploads directly: yt-dlp hands back one nested sub-playlist per tab
    # (Videos, Live, Shorts, …) instead. Follow the Videos tab so a plain
    # channel link yields every upload, not just what the tab listing
    # itself contains.
    videos_tab = next(
        (
            e for e in entries
            if e and e.get("_type") in {"playlist", "multi_video"}
            and (e.get("webpage_url") or "").rstrip("/").endswith("/videos")
        ),
        None,
    )
    if videos_tab is not None:
        return resolve_youtube(videos_tab["webpage_url"])

    title = info.get("title") or "playlist"
    tracks = []
    dropped = 0
    for entry in entries:
        if not entry:
            # Only reachable after exhausting RESOLVE_RETRIES above with the
            # gap still present — i.e. YouTube kept failing the same page.
            dropped += 1
            continue
        if entry.get("_type") in {"playlist", "multi_video"}:
            continue  # nested tab (e.g. Live, Shorts), not a video itself
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
    if dropped:
        raise RuntimeError(
            f"YouTube hat beim Auflisten {dropped} Einträge dauerhaft verweigert "
            "(vermutlich vorübergehende Ratenbegrenzung) — bitte in ein paar "
            "Minuten erneut versuchen, damit nichts fehlt."
        )
    return title, tracks, True


def _postprocessors(fmt: dict) -> list[dict]:
    pps: list[dict] = [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": fmt["codec"],
        "preferredquality": fmt["quality"],
    }]
    pps.append({"key": "FFmpegMetadata", "add_metadata": True})
    pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    return pps


def download_youtube_media(
    track: dict,
    index: int,
    dest: Path,
    fmt: dict,
    on_progress: Callable[[float], None],
) -> None:
    """Download one YouTube item into `dest`. Raises on failure."""
    # `%(title).120B` truncates the *title field* to 120 bytes. Do not use
    # yt-dlp's `trim_file_name`: it slices the whole absolute path, not the name.
    outtmpl = str(dest / f"{index:03d} - %(title).120B.%(ext)s")

    def hook(d: dict) -> None:
        if d.get("status") != "downloading":
            on_progress(1.0 if d.get("status") == "finished" else 0.0)
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        done = d.get("downloaded_bytes") or 0
        on_progress(min(done / total, 1.0) if total else 0.0)

    opts = _base_ydl_opts() | {
        "outtmpl": outtmpl,
        "progress_hooks": [hook],
        "postprocessor_hooks": [lambda d: on_progress(1.0)],
        "ignoreerrors": False,   # per-track: we want the exception
        "noplaylist": True,
        "concurrent_fragment_downloads": 4,
        # YouTube throttles a single continuous connection's rate down over
        # time (roughly: the longer one request has been running, the more
        # it gets squeezed). Splitting the same download into fresh ranged
        # requests resets that per-request throttle, which is what actually
        # moves the needle on speed here — concurrent_fragment_downloads
        # above only helps the rarer fragmented (HLS/DASH-manifest) formats;
        # most googlevideo.com audio/video URLs are one plain HTTP stream,
        # where only http_chunk_size has any effect.
        "http_chunk_size": 10 * 1024 * 1024,
    }
    if fmt["kind"] == "video":
        # Prefer separately delivered best video/audio, then a pre-muxed file.
        # ffmpeg creates a broadly compatible MP4 even when the source streams
        # arrived in different containers.
        height = int(fmt["height"])
        opts.update({
            "format": f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b",
            "merge_output_format": "mp4",
            "postprocessors": [{"key": "FFmpegMetadata", "add_metadata": True}],
        })
    else:
        opts.update({
            "format": "bestaudio/best",
            "writethumbnail": True,
            "postprocessors": _postprocessors(fmt),
        })
    with yt_dlp.YoutubeDL(opts) as ydl:
        if track.get("_info"):
            info = dict(track["_info"])
            # Resolution may have selected a video/audio pair. Let this
            # downloader select afresh for the user's format; otherwise the
            # old requested_formats can trigger video transfers for MP3 jobs.
            info.pop("requested_formats", None)
            info.pop("requested_downloads", None)
            ydl.process_ie_result(info, download=True)
        else:
            ydl.download([track["url"]])


# --------------------------------------------------------------------------- #
# Spotify (via spotDL)
# --------------------------------------------------------------------------- #

_SPOTDL_FORMATS = {"mp3", "flac", "ogg", "opus", "m4a", "wav"}
_DOWNLOADED_RE = re.compile(r'^\s*Downloaded\s+"(?P<name>.+)"', re.I)
# spotDL raises this with the exact "Artist - Title" search query it used, so a
# failed track can be identified precisely enough to retry later. A generic
# AudioProviderError carries no such query, so those failures stay unretriable.
_LOOKUP_FAILURE_RE = re.compile(r'No results found for song:\s*(?P<query>.+)$', re.I)
# spotDL logs every per-song failure twice, in two different shapes: once
# live as it happens ("<ExceptionClass>: <message>") and again in a final
# summary once the whole batch is done ("<song-url> - <ExceptionClass>:
# <message>", only with --print-errors). Both are matched here so no failure
# type is missed (earlier this only recognised "LookupError"/
# "AudioProviderError" by name, silently dropping others like a plain
# network timeout), and `_seen_failures` below dedupes the exact same
# message so it isn't counted twice. The optional prefix is deliberately
# anchored to an actual URL (not just "any word followed by ' - '"): spotDL's
# own per-song status lines ("<artist> - <title>: Downloading", "…: Done", …)
# have the exact same "<words> - <word>: <word>" shape whenever the artist
# and title are each a single word ("Eminem - Stan: Downloading"), and a
# looser prefix match was misreading those as failed downloads.
_SPOTDL_FAILURE_RE = re.compile(r'^(?:https?://\S+ - )?(?P<exc>\w+): (?P<msg>.+)$')


def _spotdl_base_cmd() -> list[str]:
    venv = Path(__file__).resolve().parent.parent / ".venv"
    runner = Path(__file__).resolve().parent / "spotdl_runner.py"
    python = venv / "bin" / "python3"
    if config.FORCE_IPV4 and python.is_file() and runner.is_file():
        # See spotdl_runner.py: without this, spotDL's YouTube Music searches
        # go out over IPv6 and intermittently come back malformed, crashing
        # most tracks in a run outright instead of just missing a match; and
        # artist resolution fetches every album one at a time.
        cmd = [str(python), str(runner)]
    else:
        exe = venv / "bin" / "spotdl"
        cmd = [str(exe) if exe.is_file() else "spotdl"]
    if config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET:
        cmd += ["--client-id", config.SPOTIFY_CLIENT_ID,
                "--client-secret", config.SPOTIFY_CLIENT_SECRET]
    if config.COOKIES_FILE.is_file():
        cmd += ["--cookie-file", str(config.COOKIES_FILE)]
    return cmd


_SPOTIFY_ID_RE = re.compile(r"^[0-9A-Za-z]{22}$")
_SPOTIFY_CACHEABLE_KINDS = {"playlist", "album", "artist"}


def _spotify_resource(url: str) -> tuple[str, str] | None:
    """('playlist'|'album'|'artist', id) for a resource worth caching, else None.

    Deliberately strict about the id shape (Spotify's own 22-char base62
    convention): a link this doesn't match cannot be looked up cheaply below
    anyway, so falling through to the normal, always-correct `spotdl save`
    path is both simpler and safer than half-running the cache logic on it.
    """
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) >= 2 and parts[0] in _SPOTIFY_CACHEABLE_KINDS and _SPOTIFY_ID_RE.match(parts[1]):
        return parts[0], parts[1]
    return None


def _spotify_cache_path(kind: str, spotify_id: str) -> Path:
    return config.SPOTIFY_CACHE_DIR / f"{kind}-{spotify_id}.json"


def _spotify_fingerprint(kind: str, spotify_id: str) -> str | None:
    """A cheap signal that changes whenever the resource's contents do.

    None means "couldn't tell" (network hiccup, API shape changed, …) — the
    caller treats that as a cache miss and falls back to the always-correct
    full resolve, so a failure here can never produce a stale/wrong result,
    only a slower one.
    """
    try:
        if kind == "playlist":
            from spotapi.playlist import PublicPlaylist
            info = PublicPlaylist(spotify_id).get_playlist_info(limit=1)
            return info["data"]["playlistV2"]["revisionId"]
        if kind == "artist":
            from spotapi.artist import Artist as SpotApiArtist
            resp = SpotApiArtist().get_artist(spotify_id)
            latest = resp["data"]["artistUnion"]["discography"].get("latest") or {}
            date = latest.get("date") or {}
            return f"{latest.get('id')}:{date.get('year')}-{date.get('month')}-{date.get('day')}"
        if kind == "album":
            # Immutable once released for all practical purposes; the TTL
            # sweep (see jobs._reap_once) is what eventually refreshes this.
            return "static"
    except Exception:  # noqa: BLE001 - any failure just means "skip the cache"
        return None
    return None


def _load_spotify_cache(kind: str, spotify_id: str) -> dict | None:
    path = _spotify_cache_path(kind, spotify_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_spotify_cache(kind: str, spotify_id: str, fingerprint: str, title: str, data: list) -> None:
    path = _spotify_cache_path(kind, spotify_id)
    tmp_path = path.with_name(path.name + f".{os.getpid()}.tmp")
    payload = {
        "kind": kind, "spotify_id": spotify_id, "fingerprint": fingerprint,
        "resolved_at": time.time(), "title": title, "data": data,
    }
    try:
        config.SPOTIFY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError:
        tmp_path.unlink(missing_ok=True)


def _tracks_from_spotdl_data(data: list) -> list[dict]:
    tracks = []
    for song in data:
        artists = song.get("artists") or []
        artist = ", ".join(artists) if artists else (song.get("artist") or "")
        name = song.get("name") or "Unbekannt"
        tracks.append({
            "title": f"{artist} – {name}" if artist else name,
            "duration": song.get("duration"),
        })
    return tracks


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


def resolve_spotify(
    url: str,
    workdir: Path,
    on_start: Callable[[subprocess.Popen], None] | None = None,
) -> tuple[str, list[dict], Path]:
    """Fetch the track list via `spotdl save`.

    Returns (title, tracks, save_file). The save file is reused for the download
    so the Spotify API is only queried once. Runs as a `Popen` (not `run`) and
    hands the process back through `on_start` so a cancel request can kill it
    even while an artist's whole discography is still being paginated — that
    step alone can take a while and would otherwise ignore Cancel completely.

    No timeout here: even with the speedups below, an unusually large
    discography can still take a while, and used to make this fail outright
    even though it was still making progress. There is no runaway risk to
    guard against — the process is bounded by the artist's (finite) catalog,
    and Cancel now reliably stops it (SIGTERM, escalating to SIGKILL) if a
    user doesn't want to wait.

    Two things made this slow enough to matter (a ~478-track artist took
    ~16 minutes before either fix): spotDL's own per-album pagination is
    one Spotify-API call at a time (patched to run concurrently, see
    spotdl_runner.py), and afterwards `spotdl save` re-fetches full metadata
    for every individual track — still one HTTP call each, but at least
    genuinely parallel across `--threads`, which is why that flag is passed
    here too instead of spotDL's low default of 4.

    Also checked first against a small on-disk cache (one JSON file per
    playlist/album/artist, see _spotify_cache*): if a cheap freshness check
    says nothing has changed since the last resolve, that cached track list
    is reused and this whole slow subprocess is skipped entirely. A miss or
    any doubt falls straight through to the full resolve below, so this can
    only make a repeat download faster, never wrong.

    The `spotdl save` subprocess itself is retried (config.RESOLVE_RETRIES,
    same knob the YouTube listing retry uses) if it exits without producing
    a save file: spotDL's Spotify API backend (spotapi, unofficial/reverse
    engineered) occasionally returns an empty or malformed body — confirmed
    live as a bare `JSONDecodeError`/`AlbumError: Could not get album info`
    killing an otherwise-fine, perfectly public playlist/artist outright.
    Re-running from scratch after a short backoff is what actually recovers
    it. Not retried when the process was killed by a signal (negative
    `returncode`) — that means Cancel, not a transient hiccup, and should
    fail fast so `check_cancelled()` below can turn it into a clean
    cancellation instead of quietly starting another expensive subprocess.
    """
    save_file = workdir / "tracks.spotdl"
    resource = _spotify_resource(url)
    fingerprint: str | None = None
    if resource is not None:
        kind, spotify_id = resource
        fingerprint = _spotify_fingerprint(kind, spotify_id)
        cached = _load_spotify_cache(kind, spotify_id) if fingerprint is not None else None
        if cached is not None and cached.get("fingerprint") == fingerprint:
            data = cached["data"]
            save_file.write_text(json.dumps(data), encoding="utf-8")
            return cached.get("title") or _spotify_title_from_url(url), _tracks_from_spotdl_data(data), save_file

    cmd = _spotdl_base_cmd() + [
        "save", url,
        "--save-file", str(save_file),
        "--threads", str(config.SPOTIFY_THREADS),
        "--log-level", "ERROR",
        "--simple-tui",
    ]
    tail = "keine Ausgabe"
    for attempt in range(1, config.RESOLVE_RETRIES + 1):
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(workdir)
        )
        if on_start is not None:
            on_start(proc)
        stdout, _ = proc.communicate()
        if save_file.is_file():
            break
        if (proc.returncode or 0) < 0:
            break  # killed by Cancel — fail fast, see docstring above
        detail = (stdout or "").strip().splitlines()
        tail = " ".join(detail[-3:]) if detail else "keine Ausgabe"
        if attempt < config.RESOLVE_RETRIES:
            time.sleep(config.RESOLVE_RETRY_DELAY * attempt)

    if not save_file.is_file():
        raise RuntimeError(f"Spotify-Playlist konnte nicht gelesen werden: {tail}")

    data = json.loads(save_file.read_text(encoding="utf-8"))
    tracks = _tracks_from_spotdl_data(data)

    title = ""
    if data:
        title = data[0].get("list_name") or data[0].get("album_name") or ""
    if not title:
        title = _spotify_title_from_url(url)

    if resource is not None and data:
        kind, spotify_id = resource
        if fingerprint is None:
            fingerprint = _spotify_fingerprint(kind, spotify_id)
        if fingerprint is not None:
            _save_spotify_cache(kind, spotify_id, fingerprint, title, data)

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
) -> list[dict]:
    """Run `spotdl download` and report progress line by line.

    Returns the tracks spotDL reported as failed, as
    `{"title": str, "spotify_query": str | None}` — `spotify_query` is set
    only when the failure line named the exact search query, which is what
    makes that one track retryable later.
    """
    codec = fmt["codec"] if fmt["codec"] in _SPOTDL_FORMATS else "mp3"
    bitrate = f'{fmt["quality"]}k' if fmt["codec"] == "mp3" else "auto"

    cmd = _spotdl_base_cmd() + [
        "download", str(save_file),
        "--output", str(dest / f"{number_field}{' - ' if number_field else ''}"
                        "{artists} - {title}.{output-ext}"),
        "--format", codec,
        "--bitrate", bitrate,
        "--threads", str(config.SPOTIFY_THREADS),
        "--max-retries", str(config.SPOTDL_MAX_RETRIES),
        "--overwrite", "skip",
        "--print-errors",
        "--simple-tui",
        "--log-level", "INFO",
        "--max-filename-length", "120",
    ]

    done = 0
    failures: list[dict] = []
    seen_failures: set[str] = set()
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
                continue
            m = _SPOTDL_FAILURE_RE.match(line)
            if m:
                msg = m.group("msg").strip()
                if msg in seen_failures:
                    continue
                seen_failures.add(msg)
                qm = _LOOKUP_FAILURE_RE.search(msg)
                query = qm.group("query").strip() if qm else None
                failures.append({"title": query or msg[:160], "spotify_query": query})
    finally:
        proc.stdout.close()
        rc = proc.wait()

    if rc != 0 and done == 0:
        raise RuntimeError("spotDL-Download fehlgeschlagen (keine Titel geladen).")
    return failures


def download_spotify_track(query: str, dest: Path, fmt: dict, timeout: int = 300) -> Path:
    """Download exactly one track by its "Artist - Title" search query.

    Used to retry a single track that failed during a full playlist run,
    without re-touching everything that already downloaded fine.
    """
    codec = fmt["codec"] if fmt["codec"] in _SPOTDL_FORMATS else "mp3"
    bitrate = f'{fmt["quality"]}k' if fmt["codec"] == "mp3" else "auto"
    cmd = _spotdl_base_cmd() + [
        "download", query,
        "--output", str(dest / "{artists} - {title}.{output-ext}"),
        "--format", codec,
        "--bitrate", bitrate,
        "--max-retries", str(config.SPOTDL_MAX_RETRIES),
        "--overwrite", "skip",
        "--print-errors",
        "--simple-tui",
        "--log-level", "ERROR",
        "--max-filename-length", "120",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=str(dest.parent),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    files = [p for p in collect_media_files(dest, "audio") if p.suffix == f".{fmt['ext']}"]
    if len(files) != 1:
        detail = (proc.stdout or proc.stderr or "").strip().splitlines()
        tail = " ".join(detail[-2:]) if detail else "kein Ergebnis"
        raise RuntimeError(f"Titel nicht gefunden: {tail}")
    return files[0]


# --------------------------------------------------------------------------- #
# Packaging
# --------------------------------------------------------------------------- #

_AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".aac", ".webm"}
_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov"}


def collect_media_files(dest: Path, kind: str) -> list[Path]:
    exts = _VIDEO_EXTS if kind == "video" else _AUDIO_EXTS
    return sorted(
        p for p in dest.rglob("*")
        if p.is_file() and p.suffix.lower() in exts
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


def append_to_zip(zip_path: Path, file: Path) -> int:
    """Add one more file to an already-built ZIP and return the new size.

    Used when a track that failed the first time round is retried after the
    job is already done; the existing ZIP gains the extra member instead of
    being rebuilt from scratch. Built on a sibling temp file and swapped in
    with `os.replace` rather than appended to the live file in place: a
    concurrent `/download` request may already have `zip_path` open and be
    streaming it (FileResponse reads until physical EOF, not up to a fixed
    length), and mutating that same file underneath it would corrupt the
    transfer. A reader that opened the file before the swap keeps reading its
    own file descriptor's old, complete content; anyone downloading after the
    swap gets the updated archive. Safe against concurrent retries of the
    same job too, since the caller holds `Job._lock` for this call.
    """
    tmp_path = zip_path.with_name(zip_path.name + ".tmp")
    shutil.copyfile(zip_path, tmp_path)
    with zipfile.ZipFile(tmp_path, "a", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        zf.write(file, file.name)
    os.replace(tmp_path, zip_path)
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
