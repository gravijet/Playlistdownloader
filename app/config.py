"""Runtime configuration, read once from the environment at import time."""
from __future__ import annotations

import os
import secrets
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# Where jobs, downloads and zips live. Must be writable by the service user.
DATA_DIR = Path(os.environ.get("YTDLWEB_DATA_DIR", "/var/lib/ytdlweb"))
JOBS_DIR = DATA_DIR / "jobs"

# Per-browser download history (small JSON file per owner id), kept separate
# from JOBS_DIR because it survives long after the job's files are reaped.
HISTORY_DIR = DATA_DIR / "history"
HISTORY_LIMIT = _int("YTDLWEB_HISTORY_LIMIT", 200)

# Optional shared password gating the whole app. Empty string disables the gate.
APP_PASSWORD = os.environ.get("YTDLWEB_PASSWORD", "").strip()

# Signing key for the session cookie. Generated per boot if unset, which simply
# means everyone has to log in again after a restart.
SECRET_KEY = os.environ.get("YTDLWEB_SECRET_KEY") or secrets.token_urlsafe(32)

# Guard rails.
MAX_TRACKS = _int("YTDLWEB_MAX_TRACKS", 5000)
MAX_CONCURRENT_JOBS = _int("YTDLWEB_MAX_CONCURRENT_JOBS", 2)
TRACK_WORKERS = max(1, min(_int("YTDLWEB_TRACK_WORKERS", 3), 6))
# spotDL's own concurrency for searching/downloading/converting tracks. Was
# fixed at 3; now that IPv6-triggered search crashes are fixed (see
# downloader._spotdl_base_cmd), more tracks in flight at once no longer just
# means more crash-and-retry storms.
SPOTIFY_THREADS = max(1, min(_int("YTDLWEB_SPOTIFY_THREADS", 6), 12))
MAX_QUEUED_JOBS = _int("YTDLWEB_MAX_QUEUED_JOBS", 20)
JOB_TTL_HOURS = _int("YTDLWEB_JOB_TTL_HOURS", 24)

# A failed YouTube track is retried this many times (in total) before it is
# given up on and marked as skipped, with a short pause between attempts.
TRACK_RETRIES = max(1, _int("YTDLWEB_TRACK_RETRIES", 4))
TRACK_RETRY_DELAY = _int("YTDLWEB_TRACK_RETRY_DELAY", 4)

# spotDL's own per-track retry count (search + download on YouTube Music).
SPOTDL_MAX_RETRIES = _int("YTDLWEB_SPOTDL_MAX_RETRIES", 5)

# A channel/playlist listing that comes back with gaps (yt-dlp swallows a
# transient page-fetch error, e.g. a 403 mid-pagination, under ignoreerrors
# and turns the whole failed page into missing entries instead of raising —
# see downloader.resolve_youtube) is retried this many times from scratch
# before giving up, with a growing pause between attempts.
RESOLVE_RETRIES = max(1, _int("YTDLWEB_RESOLVE_RETRIES", 3))
RESOLVE_RETRY_DELAY = _int("YTDLWEB_RESOLVE_RETRY_DELAY", 5)

# Cache of resolved Spotify playlist/album/artist track lists, so a repeat
# download of the same (unchanged) link skips spotDL's slow metadata fetch
# entirely. One small JSON file per resource; see downloader._spotify_cache*.
SPOTIFY_CACHE_DIR = DATA_DIR / "spotify_cache"
SPOTIFY_CACHE_TTL_DAYS = _int("YTDLWEB_SPOTIFY_CACHE_TTL_DAYS", 14)

# Datacentre IPv6 ranges are blocked by YouTube far more aggressively than IPv4,
# so downloads go out over v4 unless explicitly turned off.
FORCE_IPV4 = _bool("YTDLWEB_FORCE_IPV4", True)

# Optional Netscape cookie jar, used to get past "confirm you're not a bot".
COOKIES_FILE = DATA_DIR / "cookies.txt"

# Optional Spotify API credentials; spotDL falls back to its own defaults.
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()

# Output formats offered in the UI. Video is intentionally limited to YouTube
# sources: Spotify exposes metadata and audio tracks, but no downloadable video.
DOWNLOAD_FORMATS = {
    "mp3-320": {"label": "MP3 · 320 kbps", "kind": "audio", "codec": "mp3", "quality": "320", "ext": "mp3"},
    "mp3-192": {"label": "MP3 · 192 kbps", "kind": "audio", "codec": "mp3", "quality": "192", "ext": "mp3"},
    "mp3-128": {"label": "MP3 · 128 kbps", "kind": "audio", "codec": "mp3", "quality": "128", "ext": "mp3"},
    "m4a": {"label": "M4A · AAC (Original)", "kind": "audio", "codec": "m4a", "quality": "0", "ext": "m4a"},
    "opus": {"label": "Opus (klein & gut)", "kind": "audio", "codec": "opus", "quality": "0", "ext": "opus"},
    "flac": {"label": "FLAC (verlustfrei*)", "kind": "audio", "codec": "flac", "quality": "0", "ext": "flac"},
    "video-1080": {"label": "Video · bis 1080p", "kind": "video", "height": 1080, "ext": "mp4"},
    "video-720": {"label": "Video · bis 720p", "kind": "video", "height": 720, "ext": "mp4"},
    "video-480": {"label": "Video · bis 480p", "kind": "video", "height": 480, "ext": "mp4"},
}
DEFAULT_FORMAT = "mp3-320"
