"""Run spotDL's own CLI entry point with two fixes applied first.

Invoked as a subprocess in place of the plain `spotdl` console script (see
`downloader._spotdl_base_cmd`), only when `config.FORCE_IPV4` is set.

1. Outbound network calls pinned to IPv4.

   yt-dlp has an explicit --force-ipv4 flag; spotDL and the ytmusicapi
   library it uses for YouTube Music searches do not. Left alone, those HTTP
   calls resolve to this host's IPv6 address, and YouTube's datacenter-IPv6
   blocking is far more aggressive than its IPv4 blocking (see README's
   "Warum IPv4" section). Confirmed live: the YouTube Music "songs" search
   filter was intermittently coming back as a malformed, non-JSON HTTP
   response over IPv6 — raising a bare `json.JSONDecodeError` that crashed
   that track's whole search outright, instead of the harmless empty result
   that would let spotDL fall through to the "videos" filter and actually
   find the track. That alone was failing the large majority of tracks in a
   real Spotify-artist download. Forcing every outbound connection in this
   process to IPv4 removes the crash entirely.

2. Proper pagination and concurrent fetch when resolving an artist.

   Two separate bugs, both in `Artist.get_metadata`: first, spotDL lists an
   artist's releases via `spotify_client.artist_albums()`/`.next()`, which
   goes through the SpotipyFree backend's single "queryArtistOverview" call —
   capped by Spotify at a handful of releases per group, with its "next"
   field never actually populated, so the intended follow-up pagination
   never ran. Confirmed live: an artist with 100+ combined albums/singles
   only ever yielded the first ~20. This replaces that with spotapi's own
   discography operation, which supports real offset/limit pagination and a
   trustworthy total count, fetched directly instead of through spotDL.
   Second, once the correct release list is in hand, spotDL still calls
   `Album.from_url` once per album — one at a time, strictly sequential, with
   no option to change that. For a prolific artist (hundreds of albums) that
   alone took roughly 16 minutes in testing, before a single track could
   even start downloading. This fetches them concurrently instead.

   Fragile by nature: it reimplements spotDL's own pagination logic, so it
   can silently go stale if a future spotDL update changes that method's
   shape, or if spotapi changes its discography query. If artist downloads
   start behaving oddly after either package is upgraded, this is the first
   place to check.

3. A title-only fallback match when spotDL finds nothing "safe enough".

   spotDL rejects a YouTube Music result outright — not just scores it low,
   an unconditional `continue` — unless its "artist" is at least 70% similar
   to the Spotify artist. For an official upload that artist is the real
   channel name and matches fine; for the fan-uploaded videos that make up
   most of a niche film score's YouTube presence, the "artist" YouTube Music
   reports is the *uploader's channel name* ("Songs By Anmartevez", "Dimitris
   Peponis", …) — unrelated to the composer by construction, so this check
   fails even when the title match is exact. Confirmed live against real
   Gladiator soundtrack tracks: fan-uploaded videos titled plainly "Hans
   Zimmer - Progeny" with a correct-length duration were being rejected
   before ever being scored, and the track logged as "not found" even though
   it's plainly on YouTube.

   This does not touch spotDL's own scoring — it only runs when that primary
   pass found literally nothing, and then falls back to a title-only match
   (spotDL's own name-similarity function, no reimplemented scoring), fully
   ignoring the artist check that caused the rejection. Only engages as a
   last resort, so an already-successful match is completely unaffected;
   the accepted trade-off is that this last-resort pick occasionally lands
   on the wrong version of a song (a cover, a live take) rather than skip it
   outright.

4. Retry a YouTube Music search that crashes outright, instead of giving up.

   Even pinned to IPv4, a YouTube Music search occasionally still comes back
   as a malformed response (`json.JSONDecodeError`) or times out
   (`requests.exceptions.ReadTimeout`/`ConnectionError`) — confirmed live,
   roughly one in a few attempts, clearly transient (an identical retry
   right after usually succeeds). spotDL's own retry loop only re-attempts
   on an *empty* result, not on a raised exception, so this crashed that
   track's search immediately with no retry at all. This wraps the search
   with a few attempts on a fresh client before giving up for real.
"""
import socket

_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    return _getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only


def _patch_artist_concurrency() -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from spotdl.types.artist import Artist, ArtistError
    from spotdl.types.album import Album
    from spotdl.utils.formatter import slugify
    from spotdl.utils.spotify import SpotifyClient
    from spotapi.artist import Artist as SpotApiArtist

    # Metadata-only reads (no audio, no YouTube), so a higher concurrency
    # than track downloads get is safe here.
    ALBUM_FETCH_WORKERS = 8

    def get_metadata(url: str):
        spotify_client = SpotifyClient()

        raw_artist_meta = spotify_client.artist(url)
        if raw_artist_meta is None:
            raise ArtistError(
                "Couldn't get metadata, check if you have passed correct artist id"
            )

        # spotify_client.artist_albums()/.next() (spotDL's own, going through
        # the SpotipyFree backend here) reads a single "queryArtistOverview"
        # call that Spotify itself caps at a handful of releases per group —
        # confirmed live: 20-ish items back for an artist with 100+, and its
        # "next" field is never populated, so the `.next()` pagination loop
        # this used to run never actually fired. That silently truncated
        # every artist download to whatever fit in that first page. spotapi's
        # own discography operation genuinely paginates (real offset/limit +
        # totalCount), so it's used directly instead of going through
        # spotify_client for this part.
        artist_id = url.rstrip("/").split("/")[-1].split("?")[0]
        spot_api_artist = SpotApiArtist()
        albums: list = []
        seen_ids: set = set()
        for group in ("albums", "singles", "compilations"):
            for page in spot_api_artist.paginate_artist_discography(artist_id, section=group):
                for entry in page:
                    for release in entry.get("releases", {}).get("items", []):
                        rid = release.get("id")
                        if not rid or rid in seen_ids:
                            continue
                        seen_ids.add(rid)
                        albums.append(f"https://open.spotify.com/album/{rid}")

        if not albums:
            raise ArtistError(
                "Couldn't get albums, check if you have passed correct artist id"
            )

        songs = []
        with ThreadPoolExecutor(max_workers=ALBUM_FETCH_WORKERS) as executor:
            futures = {
                executor.submit(Album.from_url, album, fetch_songs=False): album
                for album in albums
            }
            for future in as_completed(futures):
                try:
                    songs.extend(future.result().songs)
                except Exception as exc:  # noqa: BLE001
                    # One album out of what can be 100+ occasionally comes
                    # back as a transient rate-limit/malformed response from
                    # Spotify's (undocumented) API under this concurrency —
                    # confirmed live. That must not sink every other album
                    # already fetched successfully; same isolate-and-continue
                    # approach the rest of this app takes for one bad track.
                    print(f"spotdl_runner: skipping album {futures[future]}: {exc}")

        # Faithful copy of spotDL's own (slightly odd) dedup: it checks the
        # raw name against a set of slugified names, not the slug against
        # itself. Kept as-is to match vanilla spotDL's behaviour exactly.
        songs_list = []
        songs_names: set = set()
        for song in songs:
            slug_name = slugify(song.name)
            if song.name not in songs_names:
                songs_list.append(song)
                songs_names.add(slug_name)

        metadata = {
            "name": raw_artist_meta["name"],
            "genres": raw_artist_meta["genres"],
            "url": url,
            "albums": albums,
        }
        return metadata, songs_list

    Artist.get_metadata = staticmethod(get_metadata)


def _patch_lenient_matching() -> None:
    import spotdl.providers.audio.base as audio_base
    from spotdl.utils.matching import calc_name_match

    original_order_results = audio_base.order_results

    def lenient_order_results(results, song, search_query=None):
        scored = original_order_results(results, song, search_query)
        if scored or not results:
            return scored
        # Nothing survived spotDL's own artist-match gate. Fall back to a
        # plain title match (spotDL's own similarity function) instead of
        # treating this as "not found" — see module docstring, point 3.
        fallback = {}
        for result in results:
            name_match = calc_name_match(song, result, search_query)
            if name_match > 60:
                fallback[result] = name_match
        return fallback

    audio_base.order_results = lenient_order_results


def _patch_search_retry() -> None:
    import json as json_module

    import requests
    from spotdl.providers.audio.ytmusic import YouTubeMusic

    original_get_results = YouTubeMusic.get_results
    transient_errors = (json_module.JSONDecodeError, requests.exceptions.RequestException)

    def get_results_with_retry(self, search_term, log_search_failures=True, **kwargs):
        last_exc: Exception | None = None
        for _ in range(3):
            try:
                return original_get_results(self, search_term, log_search_failures, **kwargs)
            except transient_errors as exc:
                last_exc = exc
                self.client = self._create_client()
        raise last_exc

    YouTubeMusic.get_results = get_results_with_retry


_patch_artist_concurrency()
_patch_lenient_matching()
_patch_search_retry()

from spotdl import console_entry_point  # noqa: E402

if __name__ == "__main__":
    import sys

    sys.exit(console_entry_point())
