"""Regression checks using local fixtures, never real YouTube/Spotify downloads."""
import asyncio
import functools
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave
import zipfile

TEST_DATA = tempfile.TemporaryDirectory(prefix="ytdlweb-tests-")
os.environ["YTDLWEB_DATA_DIR"] = TEST_DATA.name
os.environ["YTDLWEB_PASSWORD"] = ""

from fastapi import HTTPException
from starlette.requests import Request

from app import config, downloader, main
from app.jobs import CANCELLED, DONE, ERROR, Job, JobManager


class DownloadsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TEST_DATA.name)
        self.addCleanup(self.tmp.cleanup)
        self.settings = patch.object(config, "JOBS_DIR", Path(self.tmp.name))
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.cache_dir = patch.object(
            config, "SPOTIFY_CACHE_DIR", Path(self.tmp.name) / "spotify_cache"
        )
        self.cache_dir.start()
        self.addCleanup(self.cache_dir.stop)
        # No retries/delay by default: most tests assume a track is attempted
        # exactly once. Tests that exercise retries override these locally.
        self.retries = patch.multiple(config, TRACK_RETRIES=1, TRACK_RETRY_DELAY=0)
        self.retries.start()
        self.addCleanup(self.retries.stop)
        with patch.object(JobManager, "_start_reaper"):
            self.manager = JobManager()
        self.addCleanup(self.manager._pool.shutdown, wait=True)

    def job(self, fmt="mp3-320", source="youtube", url="https://youtu.be/test"):
        job = Job(id="0123456789abcdef", url=url, source=source, fmt_key=fmt)
        job.files_dir.mkdir(parents=True, exist_ok=True)
        self.manager._jobs[job.id] = job
        return job

    def media(self, job, name="001 - Titel.mp3", content=b"ID3-test-audio"):
        path = job.files_dir / name
        path.write_bytes(content)
        return path

    def test_single_mp3_has_no_zip_and_download_endpoint_serves_exact_bytes(self):
        job = self.job()
        job.playlist_title = "Grüße / Titel"
        self.media(job)
        self.manager._package(job)
        self.assertEqual(job.status, DONE)
        self.assertEqual(job.download_name, "Grüße _ Titel.mp3")
        self.assertEqual(job.download_type, "audio/mpeg")
        self.assertEqual(job.output_path.read_bytes(), b"ID3-test-audio")
        self.assertFalse(job.zip_path.exists())
        self.assertFalse(job.files_dir.exists())

        request = Request({"type": "http", "headers": []})
        with patch.object(main, "manager", self.manager):
            response = asyncio.run(main.download_file(request, job.id))
        self.assertEqual(response.headers["content-type"], "audio/mpeg")
        self.assertIn("filename*=UTF-8''Gr%C3%BC%C3%9Fe", response.headers["content-disposition"])
        events = []

        async def send(event):
            events.append(event)

        asyncio.run(response({"type": "http", "method": "GET"}, None, send))
        self.assertEqual(b"".join(e.get("body", b"") for e in events), b"ID3-test-audio")

    def test_playlist_with_one_title_is_still_zip(self):
        job = self.job()
        job.is_playlist = True
        self.media(job)
        self.manager._package(job)
        self.assertEqual(job.status, DONE)
        self.assertEqual(job.download_type, "application/zip")
        self.assertEqual(job.to_dict()["download_size"], job.zip_size)
        with zipfile.ZipFile(job.output_path) as archive:
            self.assertEqual(archive.namelist(), ["001 - Titel.mp3"])

    def test_explicit_video_format_downloads_directly(self):
        job = self.job("video-720")
        self.media(job, "001 - Video.mp4", b"video")
        self.manager._package(job)
        self.assertEqual(job.download_type, "video/mp4")
        self.assertTrue(job.download_name.endswith(".mp4"))

    def test_missing_conversion_is_not_served_as_mp3(self):
        job = self.job()
        self.media(job, "unconverted.webm")
        with self.assertRaises(RuntimeError):
            self.manager._package(job)
        self.assertNotEqual(job.status, DONE)

    def test_unfinished_download_returns_409(self):
        job = self.job()
        request = Request({"type": "http", "headers": []})
        with patch.object(main, "manager", self.manager), self.assertRaises(HTTPException) as caught:
            asyncio.run(main.download_file(request, job.id))
        self.assertEqual(caught.exception.status_code, 409)

    def test_resolver_keeps_single_metadata_and_collection_identity(self):
        info = {"id": "video", "title": "Single", "formats": [{"url": "media"}]}
        with patch.object(downloader.yt_dlp, "YoutubeDL") as ydl:
            ydl.return_value.__enter__.return_value.extract_info.return_value = info
            title, tracks, collection = downloader.resolve_youtube("https://youtu.be/video")
            self.assertFalse(collection)
            self.assertIs(tracks[0]["_info"], info)
            self.assertEqual(ydl.call_args.args[0]["playlistend"], config.MAX_TRACKS + 1)
            ydl.return_value.__enter__.return_value.extract_info.return_value = {
                "_type": "playlist", "title": "Playlist", "entries": [info],
            }
            _, tracks, collection = downloader.resolve_youtube("https://youtube.com/playlist?list=abc")
            self.assertTrue(collection)
            self.assertEqual(len(tracks), 1)

    def test_bare_channel_url_follows_videos_tab_for_all_uploads(self):
        # A bare channel URL resolves to one nested sub-playlist per tab
        # (Videos, Live, Shorts, …) instead of the uploads directly.
        channel_info = {
            "_type": "playlist", "title": "Kanal",
            "entries": [
                {"_type": "playlist", "title": "Kanal - Videos",
                 "webpage_url": "https://www.youtube.com/channel/UC1/videos"},
                {"_type": "playlist", "title": "Kanal - Shorts",
                 "webpage_url": "https://www.youtube.com/channel/UC1/shorts"},
            ],
        }
        videos_info = {
            "_type": "playlist", "title": "Kanal - Videos",
            "entries": [{"id": "abc", "url": "abc", "title": "Erstes Video"}],
        }
        with patch.object(downloader.yt_dlp, "YoutubeDL") as ydl:
            ydl.return_value.__enter__.return_value.extract_info.side_effect = [
                channel_info, videos_info,
            ]
            title, tracks, collection = downloader.resolve_youtube(
                "https://www.youtube.com/channel/UC1"
            )
        self.assertTrue(collection)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["title"], "Erstes Video")

    def test_playlist_pagination_gap_is_retried_then_recovers(self):
        # yt-dlp's own ignoreerrors swallows a transient page-fetch error
        # (e.g. a 403 mid-pagination on a large channel/playlist) and turns
        # the failed page into a None entry instead of raising, silently
        # truncating everything after it. A retry of the whole listing is
        # what recovers the rest once the transient block clears.
        gappy = {
            "_type": "playlist", "title": "Liste",
            "entries": [{"id": "a", "url": "a", "title": "A"}, None],
        }
        clean = {
            "_type": "playlist", "title": "Liste",
            "entries": [{"id": "a", "url": "a", "title": "A"}, {"id": "b", "url": "b", "title": "B"}],
        }
        with patch.multiple(config, RESOLVE_RETRIES=3, RESOLVE_RETRY_DELAY=0), \
                patch.object(downloader.yt_dlp, "YoutubeDL") as ydl:
            ydl.return_value.__enter__.return_value.extract_info.side_effect = [gappy, clean]
            title, tracks, collection = downloader.resolve_youtube("https://youtube.com/playlist?list=x")
        self.assertTrue(collection)
        self.assertEqual([t["title"] for t in tracks], ["A", "B"])

    def test_playlist_pagination_gap_raises_after_exhausting_retries(self):
        gappy = {
            "_type": "playlist", "title": "Liste",
            "entries": [{"id": "a", "url": "a", "title": "A"}, None],
        }
        with patch.multiple(config, RESOLVE_RETRIES=2, RESOLVE_RETRY_DELAY=0), \
                patch.object(downloader.yt_dlp, "YoutubeDL") as ydl:
            ydl.return_value.__enter__.return_value.extract_info.side_effect = [gappy, gappy]
            with self.assertRaises(RuntimeError):
                downloader.resolve_youtube("https://youtube.com/playlist?list=x")

    def test_spotify_resource_parses_only_well_formed_ids(self):
        real_id = "3TVXtAsR1Inumwj472S9r4"
        self.assertEqual(
            downloader._spotify_resource(f"https://open.spotify.com/playlist/{real_id}"),
            ("playlist", real_id),
        )
        self.assertEqual(
            downloader._spotify_resource(f"https://open.spotify.com/artist/{real_id}?si=abc"),
            ("artist", real_id),
        )
        # Too short / clearly a placeholder: skip the cache path entirely
        # rather than half-run it against something that isn't a real id.
        self.assertIsNone(downloader._spotify_resource("https://open.spotify.com/artist/test"))
        self.assertIsNone(downloader._spotify_resource("https://open.spotify.com/track/" + real_id))

    def test_spotify_cache_round_trip(self):
        data = [{"name": "Song", "artists": ["Artist"], "duration": 100, "list_name": "Liste"}]
        downloader._save_spotify_cache("playlist", "abc", "fp1", "Liste", data)
        cached = downloader._load_spotify_cache("playlist", "abc")
        self.assertEqual(cached["fingerprint"], "fp1")
        self.assertEqual(cached["data"], data)

    def test_resolve_spotify_reuses_cache_when_fingerprint_is_unchanged(self):
        real_id = "3TVXtAsR1Inumwj472S9r4"
        url = f"https://open.spotify.com/playlist/{real_id}"
        data = [{"name": "Song", "artists": ["Artist"], "duration": 100, "list_name": "Liste"}]
        downloader._save_spotify_cache("playlist", real_id, "fp-same", "Liste", data)
        with patch.object(downloader, "_spotify_fingerprint", return_value="fp-same"), \
                patch.object(downloader.subprocess, "Popen") as popen:
            title, tracks, save_file = downloader.resolve_spotify(url, Path(self.tmp.name))
        popen.assert_not_called()
        self.assertEqual(title, "Liste")
        self.assertEqual(tracks, [{"title": "Artist – Song", "duration": 100}])
        self.assertEqual(json.loads(save_file.read_text()), data)

    def test_resolve_spotify_ignores_cache_on_fingerprint_mismatch(self):
        real_id = "3TVXtAsR1Inumwj472S9r4"
        url = f"https://open.spotify.com/playlist/{real_id}"
        downloader._save_spotify_cache(
            "playlist", real_id, "fp-old", "Liste",
            [{"name": "Old", "artists": ["Artist"], "duration": 1, "list_name": "Liste"}],
        )

        workdir = Path(self.tmp.name)

        class FakeProc:
            def communicate(self):
                save_file = workdir / "tracks.spotdl"
                save_file.write_text(json.dumps(
                    [{"name": "New", "artists": ["Artist"], "duration": 2, "list_name": "Liste"}]
                ))
                return ("", None)

        with patch.object(downloader, "_spotify_fingerprint", return_value="fp-new"), \
                patch.object(downloader.subprocess, "Popen", return_value=FakeProc()) as popen:
            title, tracks, save_file = downloader.resolve_spotify(url, Path(self.tmp.name))
        popen.assert_called_once()
        self.assertEqual(tracks, [{"title": "Artist – New", "duration": 2}])

    def test_resolve_spotify_retries_transient_save_failure_then_recovers(self):
        # spotDL's Spotify API backend (spotapi, unofficial/reverse
        # engineered) occasionally comes back with an empty/malformed body
        # and crashes `spotdl save` outright for an otherwise-fine playlist —
        # confirmed live as a bare JSONDecodeError/AlbumError. A retry from
        # scratch is what actually recovers it.
        real_id = "3TVXtAsR1Inumwj472S9r4"
        url = f"https://open.spotify.com/playlist/{real_id}"
        workdir = Path(self.tmp.name)

        class FailProc:
            returncode = 1

            def communicate(self):
                return ("AlbumError: Could not get album info", None)

        class OkProc:
            returncode = 0

            def communicate(self):
                (workdir / "tracks.spotdl").write_text(json.dumps(
                    [{"name": "Song", "artists": ["Artist"], "duration": 1, "list_name": "Liste"}]
                ))
                return ("", None)

        with patch.multiple(config, RESOLVE_RETRIES=3, RESOLVE_RETRY_DELAY=0), \
                patch.object(downloader, "_spotify_fingerprint", return_value=None), \
                patch.object(downloader.subprocess, "Popen", side_effect=[FailProc(), OkProc()]) as popen:
            title, tracks, save_file = downloader.resolve_spotify(url, workdir)
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(tracks, [{"title": "Artist – Song", "duration": 1}])

    def test_resolve_spotify_raises_after_exhausting_save_retries(self):
        real_id = "3TVXtAsR1Inumwj472S9r4"
        url = f"https://open.spotify.com/playlist/{real_id}"
        workdir = Path(self.tmp.name)

        class FailProc:
            returncode = 1

            def communicate(self):
                return ("AlbumError: Could not get album info", None)

        with patch.multiple(config, RESOLVE_RETRIES=2, RESOLVE_RETRY_DELAY=0), \
                patch.object(downloader, "_spotify_fingerprint", return_value=None), \
                patch.object(downloader.subprocess, "Popen", side_effect=[FailProc(), FailProc()]) as popen:
            with self.assertRaises(RuntimeError):
                downloader.resolve_spotify(url, workdir)
        self.assertEqual(popen.call_count, 2)

    def test_resolve_spotify_does_not_retry_when_process_was_killed(self):
        # A negative returncode means the process died to a signal (Cancel's
        # SIGTERM/SIGKILL escalation, see jobs.py), not a transient API
        # hiccup — must fail fast instead of starting another subprocess.
        real_id = "3TVXtAsR1Inumwj472S9r4"
        url = f"https://open.spotify.com/playlist/{real_id}"
        workdir = Path(self.tmp.name)

        class KilledProc:
            returncode = -15

            def communicate(self):
                return ("", None)

        with patch.multiple(config, RESOLVE_RETRIES=3, RESOLVE_RETRY_DELAY=0), \
                patch.object(downloader, "_spotify_fingerprint", return_value=None), \
                patch.object(downloader.subprocess, "Popen", return_value=KilledProc()) as popen:
            with self.assertRaises(RuntimeError):
                downloader.resolve_spotify(url, workdir)
        popen.assert_called_once()

    def test_parallel_tracks_keep_order_and_skip_failed_output(self):
        job = self.job()
        tracks = [{"title": str(i), "url": str(i)} for i in range(3)]
        barrier = threading.Barrier(3)
        progress = []

        def download(track, index, dest, fmt, on_progress):
            on_progress(.5)
            barrier.wait(timeout=5)
            progress.append(job.progress)
            (dest / f"{index:03d} - {track['title']}.mp3").write_bytes(b"audio")
            if index == 2:
                raise RuntimeError("Cover conversion failed")
            on_progress(1)

        with patch.object(config, "TRACK_WORKERS", 3), \
                patch.object(downloader, "resolve_youtube", return_value=("Playlist", tracks, True)), \
                patch.object(downloader, "download_youtube_media", side_effect=download):
            self.manager._run(job)
        self.assertEqual(job.status, DONE, job.error)
        self.assertEqual(job.completed_tracks, 2)
        self.assertEqual([f["title"] for f in job.failed_tracks], ["1"])
        self.assertTrue(all(0 <= value <= .97 for value in progress))
        with zipfile.ZipFile(job.output_path) as archive:
            self.assertEqual(archive.namelist(), ["001 - 0.mp3", "003 - 2.mp3"])

    def test_parallel_youtube_downloads_show_live_current_track_and_log(self):
        # "current_track" used to stay blank for a multi-track job (which of
        # several parallel workers is "the" current one?) and log_tail only
        # ever got entries for retries/failures. Both are now populated live,
        # which is what lets the UI show what is actually happening instead
        # of a bare percentage.
        job = self.job()
        tracks = [{"title": f"Song {i}", "url": str(i)} for i in range(3)]
        barrier = threading.Barrier(3)
        seen_current_track = []

        def download(track, index, dest, fmt, on_progress):
            barrier.wait(timeout=5)
            seen_current_track.append(job.current_track)
            (dest / f"{index:03d} - {track['title']}.mp3").write_bytes(b"audio")

        with patch.object(config, "TRACK_WORKERS", 3), \
                patch.object(downloader, "resolve_youtube", return_value=("Playlist", tracks, True)), \
                patch.object(downloader, "download_youtube_media", side_effect=download):
            self.manager._run(job)
        self.assertEqual(job.status, DONE, job.error)
        # All three were in flight at once, so every worker should have seen
        # all three titles listed (order follows playlist position).
        self.assertTrue(all(s == "Song 0, Song 1, Song 2" for s in seen_current_track))
        self.assertEqual(job.current_track, "")  # nothing left in flight once done
        for title in ("Song 0", "Song 1", "Song 2"):
            self.assertIn(f"Lade: {title}", job.log_tail)
            self.assertIn(f"Fertig: {title}", job.log_tail)

    def test_log_tail_keeps_only_the_most_recent_lines(self):
        job = self.job()
        for i in range(70):
            job.note(f"line {i}")
        self.assertEqual(len(job.log_tail), 60)
        self.assertEqual(job.log_tail[0], "line 10")
        exposed = job.to_dict()["log_tail"]
        self.assertEqual(len(exposed), 20)
        self.assertEqual(exposed[-1], "line 69")

    def test_failed_track_is_retried_before_succeeding(self):
        job = self.job()
        calls = []

        def download(track, index, dest, fmt, on_progress):
            calls.append(index)
            if len(calls) < 3:
                raise RuntimeError("throttled")
            (dest / f"{index:03d} - {track['title']}.mp3").write_bytes(b"audio")

        with patch.multiple(config, TRACK_RETRIES=4, TRACK_RETRY_DELAY=0), \
                patch.object(downloader, "resolve_youtube", return_value=("Title", [{"title": "Song"}], False)), \
                patch.object(downloader, "download_youtube_media", side_effect=download):
            self.manager._run(job)
        self.assertEqual(job.status, DONE, job.error)
        self.assertEqual(job.failed_tracks, [])
        self.assertEqual(len(calls), 3)

    def test_track_is_skipped_only_after_exhausting_retries(self):
        job = self.job()
        tracks = [{"title": "Good"}, {"title": "Bad"}]
        calls = {"Good": 0, "Bad": 0}

        def download(track, index, dest, fmt, on_progress):
            calls[track["title"]] += 1
            if track["title"] == "Bad":
                raise RuntimeError("still throttled")
            (dest / f"{index:03d} - {track['title']}.mp3").write_bytes(b"audio")

        with patch.multiple(config, TRACK_RETRIES=3, TRACK_RETRY_DELAY=0), \
                patch.object(downloader, "resolve_youtube", return_value=("Title", tracks, True)), \
                patch.object(downloader, "download_youtube_media", side_effect=download):
            self.manager._run(job)
        self.assertEqual(job.status, DONE, job.error)
        self.assertEqual([f["title"] for f in job.failed_tracks], ["Bad"])
        self.assertEqual(calls, {"Good": 1, "Bad": 3})

    def test_retry_failed_track_merges_into_existing_zip(self):
        job = self.job()
        tracks = [{"title": "Good", "url": "good"}, {"title": "Bad", "url": "bad"}]

        def download(track, index, dest, fmt, on_progress):
            if track["title"] == "Bad":
                raise RuntimeError("throttled")
            (dest / f"{index:03d} - {track['title']}.mp3").write_bytes(b"audio")

        with patch.object(downloader, "resolve_youtube", return_value=("Title", tracks, True)), \
                patch.object(downloader, "download_youtube_media", side_effect=download):
            self.manager._run(job)
        self.assertEqual(job.status, DONE, job.error)
        self.assertEqual(len(job.failed_tracks), 1)
        self.assertEqual(job.to_dict()["failed_count"], 1)
        zip_size_before = job.zip_size

        def download_retry(track, index, dest, fmt, on_progress):
            (dest / f"{index:03d} - {track['title']}.mp3").write_bytes(b"audio-retried")

        with patch.object(downloader, "download_youtube_media", side_effect=download_retry):
            self.manager._retry_track(job, 0)

        self.assertTrue(job.failed_tracks[0]["resolved"])
        self.assertEqual(job.to_dict()["failed_count"], 0)
        self.assertNotIn(0, job.retrying)
        self.assertGreater(job.zip_size, zip_size_before)
        with zipfile.ZipFile(job.output_path) as archive:
            names = set(archive.namelist())
        self.assertEqual(names, {"001 - Good.mp3", "002 - Bad.mp3"})

    def test_retry_failed_track_rejects_unknown_index(self):
        job = self.job()
        job.is_playlist = True
        self.media(job)
        self.manager._package(job)
        with self.assertRaises(IndexError):
            self.manager.retry_failed_track(job, 5)

    def test_retry_failed_track_requires_a_playlist_zip(self):
        job = self.job()
        self.media(job)
        self.manager._package(job)  # single file, not a playlist
        job.failed_tracks = [{"title": "Bad", "youtube_track": {"title": "Bad", "url": "bad"}}]
        with self.assertRaises(RuntimeError):
            self.manager.retry_failed_track(job, 0)

    def test_retry_failed_track_rejects_untitled_failures(self):
        job = self.job()
        job.is_playlist = True
        self.media(job)
        self.manager._package(job)
        job.failed_tracks = [{"title": "Bad"}]  # no youtube_track or spotify_query
        with self.assertRaises(RuntimeError):
            self.manager.retry_failed_track(job, 0)

    def test_cancelled_parallel_job_has_no_download(self):
        job = self.job()

        def download(track, index, dest, fmt, on_progress):
            job._cancel.set()
            on_progress(.5)

        with patch.object(downloader, "resolve_youtube", return_value=("Title", [{"title": "Title"}], False)), \
                patch.object(downloader, "download_youtube_media", side_effect=download):
            self.manager._run(job)
        self.assertEqual(job.status, CANCELLED)
        self.assertFalse(job.files_dir.exists())
        self.assertIsNone(job.output_path)

    def test_cancel_during_youtube_resolve_does_not_wait_for_it_to_finish(self):
        # A big channel/playlist listing can take a while inside yt-dlp's
        # blocking extract_info(), which has no cooperative-cancel hook.
        # Cancel must still take effect promptly instead of only once the
        # (possibly very slow) listing itself has finished.
        job = self.job()
        started = threading.Event()

        def slow_resolve(url):
            started.set()
            time.sleep(2)
            return ("Title", [{"title": "x", "url": "x"}], True)

        with patch.object(downloader, "resolve_youtube", side_effect=slow_resolve):
            t0 = time.monotonic()
            t = threading.Thread(target=self.manager._run, args=(job,))
            t.start()
            self.assertTrue(started.wait(timeout=2))
            job._cancel.set()
            t.join(timeout=2)
            elapsed = time.monotonic() - t0
        self.assertFalse(t.is_alive())
        self.assertEqual(job.status, CANCELLED)
        self.assertLess(elapsed, 1.9)

    def test_cancel_during_spotify_resolve_stops_it_immediately(self):
        # spotDL's own metadata fetch (an artist's whole discography, say) runs
        # as a plain blocking subprocess with no process handle exposed before
        # this fix, so Cancel could not reach it until it finished on its own.
        # spotDL also installs its own SIGTERM handler that, live, was
        # observed to not take effect while deep in a network call — even a
        # raw `kill -TERM` from outside the app did nothing — so this fake
        # ignores terminate() entirely and only stops on kill(), the way the
        # real process did, to pin down that Cancel escalates to SIGKILL.
        job = self.job(source="spotify", url="https://open.spotify.com/artist/test")

        class FakeProc:
            def __init__(self):
                self.killed = threading.Event()

            def communicate(self, timeout=None):
                self.killed.wait(timeout=5)
                return ("", None)

            def terminate(self):
                pass  # ignored, like spotDL's own handler under load

            def kill(self):
                self.killed.set()

            def poll(self):
                return 0 if self.killed.is_set() else None

            def wait(self, timeout=None):
                if not self.killed.wait(timeout=timeout):
                    raise subprocess.TimeoutExpired(cmd="spotdl", timeout=timeout)
                return 0

        with patch.object(downloader.subprocess, "Popen", return_value=FakeProc()), \
                patch.object(self.manager, "CANCEL_KILL_GRACE", 0.2):
            t = threading.Thread(target=self.manager._run, args=(job,))
            t.start()
            for _ in range(50):
                if job._proc is not None:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(job._proc)
            self.assertTrue(self.manager.cancel(job.id))
            t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(job.status, CANCELLED)

    def test_all_failed_downloads_report_error(self):
        job = self.job()
        with patch.object(downloader, "resolve_youtube", return_value=("Title", [{"title": "Title"}], False)), \
                patch.object(downloader, "download_youtube_media", side_effect=RuntimeError("failed")), \
                self.assertLogs("ytdlweb.jobs", level="ERROR"):
            self.manager._run(job)
        self.assertEqual(job.status, ERROR)
        self.assertIsNone(job.output_path)

    def test_spotify_lookup_failure_is_parsed_into_a_retryable_query(self):
        class FakeProc:
            def __init__(self, text):
                self.stdout = io.StringIO(text)

            def wait(self):
                return 0

        fake = FakeProc(
            'Downloaded "Artist - OK Song"\n'
            "LookupError: No results found for song: Artist - Missing Song\n"
            "AudioProviderError: something unrelated went wrong\n"
        )
        with patch.object(downloader.subprocess, "Popen", return_value=fake):
            failures = downloader.download_spotify(
                Path("unused.spotdl"), Path(self.tmp.name), config.DOWNLOAD_FORMATS["mp3-320"],
                2, "", lambda done, name: None, lambda line: None,
            )
        self.assertEqual(failures, [
            {"title": "Artist - Missing Song", "spotify_query": "Artist - Missing Song"},
            {"title": "something unrelated went wrong", "spotify_query": None},
        ])

    def test_spotify_failure_reported_live_and_again_in_the_final_summary_counts_once(self):
        # spotDL logs every per-song failure twice: once live as it happens
        # ("<ExceptionClass>: <message>"), and again in a final summary once
        # the whole batch finishes ("<song-url> - <ExceptionClass>:
        # <message>", from --print-errors). Real Hans Zimmer discography run
        # showed this inflating failed_count past total_tracks (540 failed
        # out of 478 total) because both were counted as separate failures.
        class FakeProc:
            def __init__(self, text):
                self.stdout = io.StringIO(text)

            def wait(self):
                return 0

        fake = FakeProc(
            "LookupError: No results found for song: Artist - Missing Song\n"
            "https://open.spotify.com/track/abc - LookupError: No results found for song: Artist - Missing Song\n"
        )
        with patch.object(downloader.subprocess, "Popen", return_value=fake):
            failures = downloader.download_spotify(
                Path("unused.spotdl"), Path(self.tmp.name), config.DOWNLOAD_FORMATS["mp3-320"],
                1, "", lambda done, name: None, lambda line: None,
            )
        self.assertEqual(failures, [
            {"title": "Artist - Missing Song", "spotify_query": "Artist - Missing Song"},
        ])

    def test_spotify_failure_types_other_than_lookup_are_no_longer_silently_dropped(self):
        # Previously only lines containing the literal words "LookupError" or
        # "AudioProviderError" were recognised as failures, so a network
        # timeout or a malformed-response crash (json.JSONDecodeError) was
        # neither counted as done nor as failed — the track just vanished
        # from tracking entirely.
        class FakeProc:
            def __init__(self, text):
                self.stdout = io.StringIO(text)

            def wait(self):
                return 0

        fake = FakeProc(
            "JSONDecodeError: Expecting value: line 1 column 1 (char 0)\n"
            "https://open.spotify.com/track/def - ReadTimeout: "
            "HTTPSConnectionPool(host='music.youtube.com', port=443): Read timed out.\n"
        )
        with patch.object(downloader.subprocess, "Popen", return_value=fake):
            failures = downloader.download_spotify(
                Path("unused.spotdl"), Path(self.tmp.name), config.DOWNLOAD_FORMATS["mp3-320"],
                2, "", lambda done, name: None, lambda line: None,
            )
        self.assertEqual(len(failures), 2)
        self.assertIsNone(failures[0]["spotify_query"])
        self.assertIsNone(failures[1]["spotify_query"])

    def test_spotify_song_status_lines_are_not_mistaken_for_failures(self):
        # A song's own status updates ("<song>: Downloading", "<song>: Done", …)
        # must never be counted as a failure just because they contain a colon.
        class FakeProc:
            def __init__(self, text):
                self.stdout = io.StringIO(text)

            def wait(self):
                return 0

        fake = FakeProc(
            "Hans Zimmer - Time: Searching for song\n"
            "Hans Zimmer - Time: Downloading\n"
            'Downloaded "Hans Zimmer - Time"\n'
        )
        with patch.object(downloader.subprocess, "Popen", return_value=fake):
            failures = downloader.download_spotify(
                Path("unused.spotdl"), Path(self.tmp.name), config.DOWNLOAD_FORMATS["mp3-320"],
                1, "", lambda done, name: None, lambda line: None,
            )
        self.assertEqual(failures, [])

    def test_spotify_track_is_direct_album_and_playlist_are_zip(self):
        for kind in ("track", "album", "playlist"):
            with self.subTest(kind=kind):
                job = self.job(source="spotify", url=f"https://open.spotify.com/{kind}/test")

                def download(save_file, dest, *args, **kwargs):
                    (dest / "Track.mp3").write_bytes(b"audio")
                    return []

                with patch.object(downloader, "resolve_spotify", return_value=("Title", [{}], job.dir / "tracks.spotdl")), \
                        patch.object(downloader, "download_spotify", side_effect=download):
                    self.manager._run(job)
                self.assertEqual(job.status, DONE, job.error)
                self.assertEqual(job.is_playlist, kind != "track")
                self.assertEqual(job.download_type, "audio/mpeg" if kind == "track" else "application/zip")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
    def test_real_ytdlp_and_ffmpeg_reuse_metadata_to_create_mp3(self):
        source = Path(self.tmp.name) / "source.wav"
        with wave.open(str(source), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(22050)
            wav.writeframes(b"\0\0" * 2205)

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        handler = functools.partial(QuietHandler, directory=self.tmp.name)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/source.wav"
        info = {"id": "fixture", "title": "Local audio", "extractor": "generic",
                "webpage_url": url, "formats": [{"url": url, "ext": "wav",
                "vcodec": "none", "acodec": "pcm_s16le", "format_id": "wav"}],
                # A resolved video may carry a previous video/audio selection.
                # An MP3 job must never request this stale video URL.
                "requested_formats": [{"url": url + "-missing-video", "ext": "mp4",
                                       "format_id": "stale-video"}]}
        job = self.job()
        track = {"url": "https://youtu.be/unused", "title": "Local audio", "_info": info}
        with patch.object(downloader, "resolve_youtube", return_value=("Local audio", [track], False)):
            self.manager._run(job)
        self.assertEqual(job.status, DONE, job.error)
        self.assertGreater(job.download_size, 100)
        self.assertEqual(job.output_path.read_bytes()[:3], b"ID3")
        self.assertEqual(job.download_type, "audio/mpeg")


if __name__ == "__main__":
    unittest.main()
