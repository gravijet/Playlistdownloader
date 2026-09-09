"""In-process job queue: one job = one playlist download ending in a zip."""
from __future__ import annotations

import logging
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import config, downloader

log = logging.getLogger("ytdlweb.jobs")

QUEUED = "queued"
RESOLVING = "resolving"
DOWNLOADING = "downloading"
PACKAGING = "packaging"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"

ACTIVE_STATES = {QUEUED, RESOLVING, DOWNLOADING, PACKAGING}


class JobCancelled(Exception):
    pass


@dataclass
class Job:
    id: str
    url: str
    source: str
    fmt_key: str
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    status: str = QUEUED
    message: str = "In der Warteschlange…"
    playlist_title: str = ""
    total_tracks: int = 0
    completed_tracks: int = 0
    current_track: str = ""
    current_track_progress: float = 0.0
    failed_tracks: list[str] = field(default_factory=list)
    log_tail: list[str] = field(default_factory=list)

    zip_name: str = ""
    zip_size: int = 0
    error: str = ""

    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _proc = None  # live spotDL subprocess, so cancel can kill it
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- paths ------------------------------------------------------------- #
    @property
    def dir(self) -> Path:
        return config.JOBS_DIR / self.id

    @property
    def files_dir(self) -> Path:
        return self.dir / "files"

    @property
    def zip_path(self) -> Path:
        return self.dir / "playlist.zip"

    # -- progress ---------------------------------------------------------- #
    @property
    def progress(self) -> float:
        if self.status == DONE:
            return 1.0
        if self.status in {ERROR, CANCELLED}:
            return 0.0
        if self.status in {QUEUED, RESOLVING} or not self.total_tracks:
            return 0.0
        # Downloading occupies 0…0.97, packaging the rest.
        frac = (self.completed_tracks + self.current_track_progress) / self.total_tracks
        if self.status == PACKAGING:
            return 0.97
        return min(frac, 1.0) * 0.97

    def note(self, line: str) -> None:
        with self._lock:
            self.log_tail.append(line[:300])
            del self.log_tail[:-40]

    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "source": self.source,
            "format": self.fmt_key,
            "status": self.status,
            "message": self.message,
            "playlist_title": self.playlist_title,
            "total_tracks": self.total_tracks,
            "completed_tracks": self.completed_tracks,
            "current_track": self.current_track,
            "failed_tracks": self.failed_tracks[:50],
            "failed_count": len(self.failed_tracks),
            "progress": round(self.progress, 4),
            "zip_name": self.zip_name,
            "zip_size": self.zip_size,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "log_tail": self.log_tail[-8:],
            "expires_in": self._expires_in(),
        }

    def _expires_in(self) -> int | None:
        if self.status != DONE or self.finished_at is None:
            return None
        left = int(self.finished_at + config.JOB_TTL_HOURS * 3600 - time.time())
        return max(left, 0)


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=config.MAX_CONCURRENT_JOBS, thread_name_prefix="dl"
        )
        config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        self._start_reaper()

    # -- public API -------------------------------------------------------- #
    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.status in ACTIVE_STATES)

    def submit(self, url: str, fmt_key: str) -> Job:
        url = downloader.normalise_url(url)
        source = downloader.detect_source(url)
        if fmt_key not in config.AUDIO_FORMATS:
            fmt_key = config.DEFAULT_FORMAT

        if self.active_count() >= config.MAX_QUEUED_JOBS:
            raise RuntimeError(
                "Der Server ist gerade ausgelastet. Bitte in ein paar Minuten nochmal."
            )

        job = Job(id=uuid.uuid4().hex[:16], url=url, source=source, fmt_key=fmt_key)
        job.files_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._jobs[job.id] = job
        self._pool.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.status not in ACTIVE_STATES:
            return False
        job._cancel.set()
        proc = job._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        return True

    def delete(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        self.cancel(job_id)
        with self._lock:
            self._jobs.pop(job_id, None)
        shutil.rmtree(job.dir, ignore_errors=True)
        return True

    # -- worker ------------------------------------------------------------ #
    def _run(self, job: Job) -> None:
        try:
            job.check_cancelled()
            fmt = config.AUDIO_FORMATS[job.fmt_key]
            if job.source == "youtube":
                self._run_youtube(job, fmt)
            else:
                self._run_spotify(job, fmt)
            self._package(job)
        except JobCancelled:
            job.status = CANCELLED
            job.message = "Abgebrochen."
            job.finished_at = time.time()
            shutil.rmtree(job.files_dir, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            log.exception("job %s failed", job.id)
            job.status = ERROR
            job.error = str(exc) or exc.__class__.__name__
            job.message = "Fehlgeschlagen."
            job.finished_at = time.time()
            job.note(traceback.format_exc().splitlines()[-1])
            shutil.rmtree(job.files_dir, ignore_errors=True)

    def _run_youtube(self, job: Job, fmt: dict) -> None:
        job.status = RESOLVING
        job.message = "Playlist wird gelesen…"
        title, tracks = downloader.resolve_youtube(job.url)
        job.check_cancelled()

        if not tracks:
            raise RuntimeError("Playlist ist leer oder nicht öffentlich zugänglich.")
        if len(tracks) > config.MAX_TRACKS:
            raise RuntimeError(
                f"Playlist hat {len(tracks)} Titel, das Limit liegt bei {config.MAX_TRACKS}."
            )

        job.playlist_title = title
        job.total_tracks = len(tracks)
        job.status = DOWNLOADING
        job.message = f"{len(tracks)} Titel werden geladen…"

        for i, track in enumerate(tracks, start=1):
            job.check_cancelled()
            job.current_track = track["title"]
            job.current_track_progress = 0.0

            def on_progress(frac: float) -> None:
                job.current_track_progress = frac

            try:
                downloader.download_youtube_track(
                    track, i, job.files_dir, fmt, on_progress
                )
            except downloader.yt_dlp.utils.DownloadError as exc:
                job.failed_tracks.append(track["title"])
                job.note(f"übersprungen: {track['title']} – {str(exc)[:160]}")
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, JobCancelled):
                    raise
                job.failed_tracks.append(track["title"])
                job.note(f"übersprungen: {track['title']} – {str(exc)[:160]}")
            finally:
                job.completed_tracks = i
                job.current_track_progress = 0.0

    def _run_spotify(self, job: Job, fmt: dict) -> None:
        job.status = RESOLVING
        job.message = "Spotify-Playlist wird gelesen…"
        title, tracks, save_file = downloader.resolve_spotify(job.url, job.dir)
        job.check_cancelled()

        if not tracks:
            raise RuntimeError("Playlist ist leer oder nicht öffentlich zugänglich.")
        if len(tracks) > config.MAX_TRACKS:
            raise RuntimeError(
                f"Playlist hat {len(tracks)} Titel, das Limit liegt bei {config.MAX_TRACKS}."
            )

        job.playlist_title = title
        job.total_tracks = len(tracks)
        job.status = DOWNLOADING
        job.message = f"{len(tracks)} Titel werden über YouTube Music geladen…"

        def on_track_done(done: int, name: str) -> None:
            job.completed_tracks = done
            job.current_track = name

        def on_line(line: str) -> None:
            job.note(line)
            if job._cancel.is_set() and job._proc is not None:
                try:
                    job._proc.terminate()
                except OSError:
                    pass

        failures = downloader.download_spotify(
            save_file, job.files_dir, fmt, len(tracks),
            downloader.spotify_number_field(job.url),
            on_track_done, on_line,
            on_start=lambda proc: setattr(job, "_proc", proc),
        )
        job._proc = None
        job.check_cancelled()
        job.failed_tracks.extend(failures)

    def _package(self, job: Job) -> None:
        job.check_cancelled()
        job.status = PACKAGING
        job.message = "ZIP wird gepackt…"
        job.current_track = ""

        downloader.cleanup_partials(job.files_dir)
        files = downloader.collect_audio_files(job.files_dir)
        if not files:
            raise RuntimeError(
                "Es konnte kein einziger Titel geladen werden. "
                "Playlist privat, regionsgesperrt oder YouTube blockt den Server."
            )

        base = downloader.safe_name(job.playlist_title, fallback=f"playlist-{job.id[:6]}")
        size = downloader.make_zip(files, job.files_dir, job.zip_path)

        job.zip_name = f"{base}.zip"
        job.zip_size = size
        job.completed_tracks = len(files)
        job.status = DONE
        job.finished_at = time.time()
        ok = len(files)
        job.message = (
            f"Fertig: {ok} Titel"
            + (f", {len(job.failed_tracks)} übersprungen" if job.failed_tracks else "")
        )
        # The audio files are only needed for the zip.
        shutil.rmtree(job.files_dir, ignore_errors=True)

    # -- housekeeping ------------------------------------------------------ #
    def _start_reaper(self) -> None:
        def reap() -> None:
            while True:
                time.sleep(300)
                try:
                    self._reap_once()
                except Exception:  # noqa: BLE001
                    log.exception("reaper failed")

        threading.Thread(target=reap, name="reaper", daemon=True).start()

    def _reap_once(self) -> None:
        cutoff = time.time() - config.JOB_TTL_HOURS * 3600
        with self._lock:
            stale = [
                j for j in self._jobs.values()
                if j.status not in ACTIVE_STATES
                and (j.finished_at or j.created_at) < cutoff
            ]
            for j in stale:
                self._jobs.pop(j.id, None)
        for j in stale:
            shutil.rmtree(j.dir, ignore_errors=True)
            log.info("reaped job %s", j.id)

        # Orphaned directories from a previous process.
        known = set(self._jobs)
        for d in config.JOBS_DIR.iterdir() if config.JOBS_DIR.is_dir() else []:
            if d.is_dir() and d.name not in known and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)


manager = JobManager()
