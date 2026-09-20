"""In-process jobs producing a media file or a ZIP for collections."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    owner: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    status: str = QUEUED
    message: str = "In der Warteschlange…"
    playlist_title: str = ""
    total_tracks: int = 0
    completed_tracks: int = 0
    current_track: str = ""
    current_track_progress: float = 0.0
    # Each entry: {"title", and either "youtube_track" (the original track
    # dict, for an exact redownload) or "spotify_query" (a free-text search
    # query) when the failure is precise enough to retry — plus "resolved"
    # once a later retry has filled it in. Never reordered/removed, so an
    # "index" handed to the UI stays valid for the job's whole lifetime.
    failed_tracks: list[dict] = field(default_factory=list)
    retrying: set[int] = field(default_factory=set, repr=False)
    log_tail: list[str] = field(default_factory=list)

    zip_name: str = ""
    zip_size: int = 0
    is_playlist: bool = False
    download_name: str = ""
    download_size: int = 0
    download_type: str = ""
    output_path: Path | None = None
    error: str = ""

    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _delete_requested: bool = field(default=False, repr=False)
    _proc = None  # live spotDL subprocess, so cancel can kill it
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _track_progress: dict[int, float] = field(default_factory=dict, repr=False)

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
            del self.log_tail[:-60]

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
            "failed": [
                {
                    "index": i,
                    "title": f["title"],
                    "retryable": bool(f.get("youtube_track") or f.get("spotify_query")),
                    "retrying": i in self.retrying,
                }
                for i, f in enumerate(self.failed_tracks)
                if not f.get("resolved")
            ][:50],
            "failed_count": sum(1 for f in self.failed_tracks if not f.get("resolved")),
            "progress": round(self.progress, 4),
            "zip_name": self.zip_name,
            "zip_size": self.zip_size,
            "is_playlist": self.is_playlist,
            "download_name": self.download_name,
            "download_size": self.download_size,
            "download_type": self.download_type,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "log_tail": self.log_tail[-20:],
            "expires_in": self._expires_in(),
        }

    def _expires_in(self) -> int | None:
        if self.status != DONE or self.finished_at is None:
            return None
        left = int(self.finished_at + config.JOB_TTL_HOURS * 3600 - time.time())
        return max(left, 0)


class JobManager:
    # How long a cancelled process gets to exit after SIGTERM before SIGKILL.
    # A class attribute (not a default argument) so tests can shrink it.
    CANCEL_KILL_GRACE = 3.0

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=config.MAX_CONCURRENT_JOBS, thread_name_prefix="dl"
        )
        # Separate from `_pool`: a single-track retry is cheap and the user
        # is actively waiting on it, so it must not queue behind whatever
        # long-running playlist downloads already occupy every `_pool`
        # worker (MAX_CONCURRENT_JOBS defaults to just 2).
        self._retry_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="retry")
        self._history_lock = threading.Lock()
        config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        self._start_reaper()

    # -- public API -------------------------------------------------------- #
    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.status in ACTIVE_STATES)

    def submit(self, url: str, fmt_key: str, owner: str = "") -> Job:
        url = downloader.normalise_url(url)
        source = downloader.detect_source(url)
        if fmt_key not in config.DOWNLOAD_FORMATS:
            fmt_key = config.DEFAULT_FORMAT
        if source == "spotify" and config.DOWNLOAD_FORMATS[fmt_key]["kind"] == "video":
            raise RuntimeError(
                "Video-Downloads sind nur für YouTube- und YouTube-Music-Links verfügbar."
            )

        if self.active_count() >= config.MAX_QUEUED_JOBS:
            raise RuntimeError(
                "Der Server ist gerade ausgelastet. Bitte in ein paar Minuten nochmal."
            )

        job = Job(id=uuid.uuid4().hex[:16], url=url, source=source, fmt_key=fmt_key, owner=owner)
        job.files_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._jobs[job.id] = job
        self._pool.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_for_owner(self, owner: str) -> list[dict]:
        """Jobs from this browser still in memory (active, or done within the TTL)."""
        if not owner:
            return []
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.owner == owner]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return [j.to_dict() for j in jobs]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.status not in ACTIVE_STATES:
            return False
        job._cancel.set()
        self._terminate_then_kill(job._proc)
        return True

    def _terminate_then_kill(self, proc, grace: float | None = None) -> None:
        """SIGTERM, then SIGKILL shortly after if that alone didn't stop it.

        spotDL installs its own SIGTERM handler for a clean shutdown, but that
        handler only runs once control returns to its main thread — while it
        is deep in paginating a prolific artist's whole discography (one
        blocking Spotify-API call after another) that can take much longer
        than a user waiting on Cancel is willing to. Escalating to SIGKILL is
        what actually makes Cancel reliable in that case.
        """
        if proc is None or proc.poll() is not None:
            return
        if getattr(proc, "_kill_escalation_started", False):
            # `cancel()` calls this once, and _run_spotify's on_line calls it
            # again for every line the subprocess still prints before it
            # actually exits — without this guard, each of those spawns its
            # own redundant escalation thread all racing to kill the same
            # process.
            return
        proc._kill_escalation_started = True
        grace = self.CANCEL_KILL_GRACE if grace is None else grace
        try:
            proc.terminate()
        except OSError:
            return

        def escalate() -> None:
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=escalate, name="cancel-kill", daemon=True).start()

    def delete(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        job._delete_requested = True
        active = self.cancel(job_id)
        with self._lock:
            self._jobs.pop(job_id, None)
        if not active:
            shutil.rmtree(job.dir, ignore_errors=True)
        return True

    # -- shared download helpers --------------------------------------------#
    def _cancellable(self, job: Job, fn, *args, **kwargs):
        """Run a blocking call that has no cooperative-cancel hook (yt-dlp's
        `extract_info` mid-playlist) without making Cancel wait for it to
        finish. The call keeps running to completion in the background — it
        cannot be interrupted from outside — but a cancel request raises
        `JobCancelled` here right away instead of only after it returns, so a
        big channel/playlist listing no longer makes the button do nothing
        for however long the listing takes.
        """
        box: dict = {}

        def target() -> None:
            try:
                box["value"] = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc

        t = threading.Thread(target=target, name="resolve", daemon=True)
        t.start()
        while t.is_alive():
            if job._cancel.wait(0.25):
                raise JobCancelled()
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _download_youtube_track_with_retries(
        self,
        track: dict,
        index: int,
        staging: Path,
        fmt: dict,
        on_progress,
        cancel: threading.Event | None = None,
        note=None,
    ) -> Path:
        """Try up to `config.TRACK_RETRIES` times; return the finished file or raise."""
        last_exc: Exception | None = None
        for attempt in range(1, config.TRACK_RETRIES + 1):
            if cancel is not None and cancel.is_set():
                raise JobCancelled()
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            on_progress(0.0)
            # A single (non-playlist) track carries its metadata from the
            # initial resolve, including the exact stream URLs it picked
            # (see download_youtube_media). Reusing that on a retry means an
            # attempt that failed for a reason tied to those specific URLs
            # fails identically every time; dropping it after attempt 1 makes
            # every retry re-extract from track["url"] instead, same as a
            # playlist track always does, so it can actually pick something
            # different on retry.
            attempt_track = track if attempt == 1 else {k: v for k, v in track.items() if k != "_info"}
            try:
                downloader.download_youtube_media(attempt_track, index, staging, fmt, on_progress)
                files = downloader.collect_media_files(staging, fmt["kind"])
                if fmt["kind"] == "audio":
                    files = [p for p in files if p.suffix == f".{fmt['ext']}"]
                if len(files) != 1:
                    raise RuntimeError("Keine eindeutige fertige Mediendatei gefunden.")
                return files[0]
            except JobCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < config.TRACK_RETRIES:
                    if note is not None:
                        note(attempt, exc)
                    if cancel is not None:
                        if cancel.wait(config.TRACK_RETRY_DELAY):
                            raise JobCancelled()
                    else:
                        time.sleep(config.TRACK_RETRY_DELAY)
        raise last_exc

    # -- worker ------------------------------------------------------------ #
    def _run(self, job: Job) -> None:
        try:
            job.check_cancelled()
            fmt = config.DOWNLOAD_FORMATS[job.fmt_key]
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
        finally:
            self._record_history(job)
            if job._delete_requested:
                shutil.rmtree(job.dir, ignore_errors=True)

    def _run_youtube(self, job: Job, fmt: dict) -> None:
        job.status = RESOLVING
        job.message = "Link wird gelesen…"
        title, tracks, job.is_playlist = self._cancellable(job, downloader.resolve_youtube, job.url)
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

        def refresh_current_track() -> None:
            # Called with job._lock held. Titles of every track a worker is
            # presently on, in playlist order — the only way to see what a
            # multi-track job is actually doing right now, since parallel
            # workers make any single "current" track meaningless.
            job.current_track = ", ".join(tracks[j - 1]["title"] for j in sorted(job._track_progress))

        def download_track(i: int, track: dict) -> None:
            job.check_cancelled()
            staging = job.dir / "staging" / str(i)

            def on_progress(frac: float) -> None:
                job.check_cancelled()
                with job._lock:
                    # A plain assignment, not max(): a retried attempt starts
                    # over from 0 (see _download_youtube_track_with_retries),
                    # and that reset must actually show up here — yt-dlp's
                    # hook only ever reports "downloading" (byte counts that
                    # only grow within one attempt) and "finished" (1.0), so
                    # there's no legitimate in-attempt dip to guard against.
                    job._track_progress[i] = frac
                    job.current_track_progress = sum(job._track_progress.values())
                    refresh_current_track()

            job.note(f"Lade: {track['title']}")

            def note_attempt(attempt: int, exc: Exception) -> None:
                job.note(
                    f"Versuch {attempt}/{config.TRACK_RETRIES} fehlgeschlagen "
                    f"({track['title']}), erneuter Versuch…"
                )

            try:
                file = self._download_youtube_track_with_retries(
                    track, i, staging, fmt, on_progress, cancel=job._cancel, note=note_attempt
                )
                shutil.move(str(file), job.files_dir / file.name)
                job.note(f"Fertig: {track['title']}")
            except JobCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                with job._lock:
                    job.failed_tracks.append(
                        {"title": track["title"], "youtube_track": track, "index_in_playlist": i}
                    )
                job.note(f"übersprungen: {track['title']} – {str(exc)[:160]}")

            with job._lock:
                job.completed_tracks += 1
                job._track_progress.pop(i, None)
                job.current_track_progress = sum(job._track_progress.values())
                refresh_current_track()
            shutil.rmtree(staging, ignore_errors=True)

        with ThreadPoolExecutor(max_workers=config.TRACK_WORKERS) as pool:
            futures = [pool.submit(download_track, i, track)
                       for i, track in enumerate(tracks, start=1)]
            try:
                for future in as_completed(futures):
                    future.result()
                    job.check_cancelled()
            finally:
                for future in futures:
                    future.cancel()

    def _run_spotify(self, job: Job, fmt: dict) -> None:
        job.status = RESOLVING
        job.message = "Spotify-Playlist wird gelesen…"
        try:
            title, tracks, save_file = downloader.resolve_spotify(
                job.url, job.dir, on_start=lambda proc: setattr(job, "_proc", proc)
            )
        except Exception:
            job._proc = None
            job.check_cancelled()  # a cancel-induced failure is a cancellation, not an error
            raise
        job._proc = None
        job.is_playlist = bool(downloader.spotify_number_field(job.url)) or len(tracks) > 1
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
            if job._cancel.is_set():
                self._terminate_then_kill(job._proc)

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
        fmt = config.DOWNLOAD_FORMATS[job.fmt_key]
        job.current_track = ""

        downloader.cleanup_partials(job.files_dir)
        files = downloader.collect_media_files(job.files_dir, fmt["kind"])
        if fmt["kind"] == "audio":
            files = [p for p in files if p.suffix == f".{fmt['ext']}"]
        if not files:
            raise RuntimeError(
                "Es konnte kein einziger Titel geladen werden. "
                "Playlist privat, regionsgesperrt oder YouTube blockt den Server."
            )

        base = downloader.safe_name(job.playlist_title, fallback=f"playlist-{job.id[:6]}")
        if job.is_playlist:
            job.status = PACKAGING
            job.message = "ZIP wird gepackt…"
            job.download_size = downloader.make_zip(files, job.files_dir, job.zip_path)
            suffix = "-video" if fmt["kind"] == "video" else ""
            job.download_name = f"{base}{suffix}.zip"
            job.download_type = "application/zip"
            job.output_path = job.zip_path
            job.zip_name = job.download_name
            job.zip_size = job.download_size
        else:
            if len(files) != 1:
                raise RuntimeError("Für diesen Link wurde mehr als eine Datei gefunden.")
            ext = files[0].suffix.lower()
            job.download_name = f"{base}{ext}"
            job.output_path = job.dir / f"download{ext}"
            # Same filesystem: moving avoids copying the entire media file.
            files[0].replace(job.output_path)
            job.download_size = job.output_path.stat().st_size
            job.download_type = {
                ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".opus": "audio/ogg",
                ".ogg": "audio/ogg", ".flac": "audio/flac", ".mp4": "video/mp4",
                ".webm": "video/webm", ".mkv": "video/x-matroska",
            }.get(ext, "application/octet-stream")

        job.check_cancelled()
        job.completed_tracks = len(files)
        job.status = DONE
        job.finished_at = time.time()
        self._refresh_done_message(job, fmt)
        # The final artifact lives outside this working directory.
        shutil.rmtree(job.files_dir, ignore_errors=True)

    def _refresh_done_message(self, job: Job, fmt: dict) -> None:
        unit = "Videos" if fmt["kind"] == "video" else "Titel"
        remaining = sum(1 for f in job.failed_tracks if not f.get("resolved"))
        job.message = (
            f"Fertig: {job.completed_tracks} {unit}"
            + (f", {remaining} übersprungen" if remaining else "")
        )

    # -- history ------------------------------------------------------------ #
    def _history_path(self, owner: str) -> Path:
        return config.HISTORY_DIR / f"{owner}.json"

    def _record_history(self, job: Job) -> None:
        """Append this finished job to its owner's persistent history log.

        Kept separate from the in-memory `Job`, which the reaper discards once
        its files expire: the history entry is what lets a browser see what it
        downloaded long after the file itself is gone.
        """
        if not job.owner or job.status not in {DONE, ERROR, CANCELLED}:
            return
        entry = {
            "id": job.id,
            "url": job.url,
            "source": job.source,
            "format": job.fmt_key,
            "status": job.status,
            "playlist_title": job.playlist_title,
            "total_tracks": job.total_tracks,
            "failed_count": sum(1 for f in job.failed_tracks if not f.get("resolved")),
            "is_playlist": job.is_playlist,
            "download_name": job.download_name,
            "download_size": job.download_size,
            "error": job.error,
            "created_at": job.created_at,
            "finished_at": job.finished_at,
        }
        path = self._history_path(job.owner)
        with self._history_lock:
            try:
                items = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
            except (OSError, ValueError):
                items = []
            items = [it for it in items if it.get("id") != job.id]
            items.append(entry)
            items = items[-config.HISTORY_LIMIT:]
            config.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(items), encoding="utf-8")

    def history_for(self, owner: str) -> list[dict]:
        """This browser's full download history, newest first.

        Entries whose job is still in memory with its file on disk are marked
        `available` so the UI can offer the download link again; everything
        else is metadata only (the file was already cleaned up).
        """
        if not owner:
            return []
        path = self._history_path(owner)
        # Same lock `_record_history` writes under: without it, a read
        # landing mid-write could see a half-written file and, via the
        # except below, hand back an empty history even though real entries
        # exist on disk.
        with self._history_lock:
            try:
                items = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
            except (OSError, ValueError):
                items = []
        for entry in items:
            job = self.get(entry["id"])
            available = bool(
                job and job.status == DONE and job.output_path and job.output_path.is_file()
            )
            entry["available"] = available
            entry["expires_in"] = job._expires_in() if available else None
        items.sort(key=lambda it: it.get("created_at", 0), reverse=True)
        return items

    # -- retrying a single skipped track after the job is done -------------- #
    def retry_failed_track(self, job: Job, index: int) -> None:
        """Validate and kick off a background retry of one skipped track.

        Only makes sense for a finished playlist/album ZIP: a single-file job
        that failed has nothing to merge into, and re-submitting the same
        link is already the way to redo it.
        """
        if job.status != DONE or not job.is_playlist:
            raise RuntimeError(
                "Nur abgeschlossene Playlist- oder Album-Downloads können einzelne Titel nachladen."
            )
        if not job.output_path or not job.output_path.is_file():
            raise RuntimeError("Die ZIP-Datei ist nicht mehr verfügbar.")
        with job._lock:
            if not (0 <= index < len(job.failed_tracks)):
                raise IndexError("Unbekannter Titel.")
            entry = job.failed_tracks[index]
            if entry.get("resolved"):
                raise RuntimeError("Titel wurde bereits nachgeladen.")
            if index in job.retrying:
                raise RuntimeError("Wird bereits erneut versucht.")
            if not (entry.get("youtube_track") or entry.get("spotify_query")):
                raise RuntimeError("Dieser Titel kann nicht automatisch neu geladen werden.")
            job.retrying.add(index)
        self._retry_pool.submit(self._retry_track, job, index)

    def _retry_track(self, job: Job, index: int) -> None:
        entry = job.failed_tracks[index]
        fmt = config.DOWNLOAD_FORMATS[job.fmt_key]
        staging = job.dir / "retry" / str(index)
        try:
            if entry.get("youtube_track") is not None:
                note = lambda attempt, exc: job.note(  # noqa: E731
                    f"Versuch {attempt}/{config.TRACK_RETRIES} fehlgeschlagen "
                    f"(Nachladen {entry['title']})"
                )
                file = self._download_youtube_track_with_retries(
                    entry["youtube_track"], entry.get("index_in_playlist", 0),
                    staging, fmt, on_progress=lambda frac: None, note=note,
                )
            else:
                staging.mkdir(parents=True, exist_ok=True)
                file = downloader.download_spotify_track(entry["spotify_query"], staging, fmt)

            with job._lock:
                if not job.output_path or not job.output_path.is_file():
                    raise RuntimeError("Die ZIP-Datei ist nicht mehr verfügbar.")
                job.download_size = downloader.append_to_zip(job.output_path, file)
                job.zip_size = job.download_size
                job.completed_tracks += 1
                entry["resolved"] = True
            self._refresh_done_message(job, fmt)
            self._record_history(job)
            job.note(f"Nachgeladen: {entry['title']}")
        except Exception as exc:  # noqa: BLE001
            job.note(f"Nachladen fehlgeschlagen ({entry['title']}): {str(exc)[:160]}")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            with job._lock:
                job.retrying.discard(index)

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

        self._reap_spotify_cache()

    def _reap_spotify_cache(self) -> None:
        """Drop resolved-Spotify-link cache files nobody has touched in a while.

        Keeps downloader._save_spotify_cache's output from growing forever —
        one small file per distinct playlist/album/artist link ever
        downloaded. A cache miss just falls back to a normal full resolve, so
        deleting one early is never wrong, only occasionally slower.
        """
        cutoff = time.time() - config.SPOTIFY_CACHE_TTL_DAYS * 86400
        cache_dir = config.SPOTIFY_CACHE_DIR
        if not cache_dir.is_dir():
            return
        for f in cache_dir.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass


manager = JobManager()
