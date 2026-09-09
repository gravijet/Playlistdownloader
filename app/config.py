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

# Optional shared password gating the whole app. Empty string disables the gate.
APP_PASSWORD = os.environ.get("YTDLWEB_PASSWORD", "").strip()

# Signing key for the session cookie. Generated per boot if unset, which simply
# means everyone has to log in again after a restart.
SECRET_KEY = os.environ.get("YTDLWEB_SECRET_KEY") or secrets.token_urlsafe(32)

# Guard rails.
MAX_TRACKS = _int("YTDLWEB_MAX_TRACKS", 300)
MAX_CONCURRENT_JOBS = _int("YTDLWEB_MAX_CONCURRENT_JOBS", 2)
MAX_QUEUED_JOBS = _int("YTDLWEB_MAX_QUEUED_JOBS", 20)
JOB_TTL_HOURS = _int("YTDLWEB_JOB_TTL_HOURS", 3)

# Datacentre IPv6 ranges are blocked by YouTube far more aggressively than IPv4,
# so downloads go out over v4 unless explicitly turned off.
FORCE_IPV4 = _bool("YTDLWEB_FORCE_IPV4", True)

# Optional Netscape cookie jar, used to get past "confirm you're not a bot".
COOKIES_FILE = DATA_DIR / "cookies.txt"

# Optional Spotify API credentials; spotDL falls back to its own defaults.
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()

# Audio formats offered in the UI, mapped to yt-dlp / spotDL settings.
AUDIO_FORMATS = {
    "mp3-320": {"label": "MP3 · 320 kbps", "codec": "mp3", "quality": "320", "ext": "mp3"},
    "mp3-192": {"label": "MP3 · 192 kbps", "codec": "mp3", "quality": "192", "ext": "mp3"},
    "mp3-128": {"label": "MP3 · 128 kbps", "codec": "mp3", "quality": "128", "ext": "mp3"},
    "m4a": {"label": "M4A · AAC (Original)", "codec": "m4a", "quality": "0", "ext": "m4a"},
    "opus": {"label": "Opus (klein & gut)", "codec": "opus", "quality": "0", "ext": "opus"},
    "flac": {"label": "FLAC (verlustfrei*)", "codec": "flac", "quality": "0", "ext": "flac"},
}
DEFAULT_FORMAT = "mp3-320"
