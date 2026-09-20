"""FastAPI application: thin HTTP layer over the job manager."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import config, downloader
from .jobs import DONE, manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("ytdlweb")

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "ytdlweb_session"
COOKIE_MAX_AGE = 30 * 24 * 3600

# Anonymous per-browser identity, independent of the login gate (which can be
# disabled entirely). It is what "your downloads" / "your history" means:
# nothing more than a random id set once and echoed back on every request.
UID_COOKIE = "ytdlweb_uid"
UID_MAX_AGE = 400 * 24 * 3600
_UID_RE = re.compile(r"^[a-f0-9]{32}$")

app = FastAPI(title="Playlist Downloader", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_signer = URLSafeTimedSerializer(config.SECRET_KEY, salt="ytdlweb-session")

# Per-IP sliding window for job creation.
_recent: dict[str, deque[float]] = defaultdict(deque)
RATE_LIMIT = 10          # jobs …
RATE_WINDOW = 600.0      # … per 10 minutes


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def client_ip(request: Request) -> str:
    # nginx rewrites the real IP from Cloudflare before proxying, so client.host
    # is already the visitor. X-Forwarded-For is the fallback.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def is_authed(request: Request) -> bool:
    if not config.APP_PASSWORD:
        return True
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        _signer.loads(token, max_age=COOKIE_MAX_AGE)
    except BadSignature:
        return False
    except Exception:  # noqa: BLE001 - expired
        return False
    return True


def require_auth(request: Request) -> None:
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="Nicht angemeldet.")


def check_rate(request: Request) -> None:
    ip = client_ip(request)
    now = time.monotonic()
    hits = _recent[ip]
    while hits and now - hits[0] > RATE_WINDOW:
        hits.popleft()
    if len(hits) >= RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="Zu viele Downloads. Bitte ein paar Minuten warten.",
        )
    hits.append(now)


def request_uid(request: Request) -> str:
    """The caller's browser id, if it sent a well-formed one. Never mints one."""
    uid = request.cookies.get(UID_COOKIE, "")
    return uid if _UID_RE.match(uid) else ""


def resolve_uid(request: Request) -> str:
    """The caller's browser id, minting one if it doesn't have one yet."""
    return request_uid(request) or secrets.token_hex(16)


def set_uid_cookie(response: Response, uid: str) -> None:
    response.set_cookie(
        UID_COOKIE, uid,
        max_age=UID_MAX_AGE, httponly=True, samesite="lax", secure=True, path="/",
    )


def owned_job(request: Request, job_id: str):
    require_auth(request)
    job = manager.get(job_id)
    if job is None or (job.owner and job.owner != request_uid(request)):
        raise HTTPException(status_code=404, detail="Job nicht gefunden oder abgelaufen.")
    return job


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

@app.get("/", include_in_schema=False)
async def index(request: Request) -> Response:
    authed = is_authed(request)
    resp = FileResponse(
        STATIC_DIR / ("index.html" if authed else "login.html"),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )
    if authed:
        set_uid_cookie(resp, resolve_uid(request))
    return resp


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "active_jobs": manager.active_count()})


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

@app.post("/api/login")
async def login(request: Request) -> JSONResponse:
    if not config.APP_PASSWORD:
        return JSONResponse({"ok": True})
    body = await request.json()
    supplied = str(body.get("password", ""))
    # Length-independent comparison.
    import hmac
    if not hmac.compare_digest(supplied, config.APP_PASSWORD):
        await asyncio.sleep(1.0)  # keep other requests responsive during the delay
        raise HTTPException(status_code=401, detail="Falsches Passwort.")

    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        COOKIE_NAME,
        _signer.dumps({"t": time.time()}),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=True,
        path="/",
    )
    return resp


@app.post("/api/logout")
async def logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #

@app.get("/api/config")
async def get_config(request: Request) -> JSONResponse:
    require_auth(request)
    return JSONResponse({
        "formats": [
            {"key": k, "label": v["label"], "kind": v["kind"]}
            for k, v in config.DOWNLOAD_FORMATS.items()
        ],
        "default_format": config.DEFAULT_FORMAT,
        "max_tracks": config.MAX_TRACKS,
        "ttl_hours": config.JOB_TTL_HOURS,
    })


@app.post("/api/jobs")
async def create_job(request: Request) -> JSONResponse:
    require_auth(request)
    check_rate(request)
    body = await request.json()
    url = str(body.get("url", "")).strip()
    fmt = str(body.get("format", config.DEFAULT_FORMAT))
    uid = resolve_uid(request)

    try:
        job = manager.submit(url, fmt, owner=uid)
    except downloader.UnsupportedURL as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    log.info("job %s created for %s (%s)", job.id, url, job.source)
    resp = JSONResponse(job.to_dict(), status_code=201)
    set_uid_cookie(resp, uid)
    return resp


@app.get("/api/jobs")
async def list_jobs(request: Request) -> JSONResponse:
    """Every job this browser started that's still on the server (active, or
    finished within the last `JOB_TTL_HOURS`) — what a page reload restores."""
    require_auth(request)
    uid = resolve_uid(request)
    resp = JSONResponse(
        {"jobs": manager.list_for_owner(uid)}, headers={"Cache-Control": "no-store"}
    )
    set_uid_cookie(resp, uid)
    return resp


@app.get("/api/history")
async def history(request: Request) -> JSONResponse:
    """This browser's full download history, including expired entries."""
    require_auth(request)
    uid = resolve_uid(request)
    resp = JSONResponse(
        {"items": manager.history_for(uid)}, headers={"Cache-Control": "no-store"}
    )
    set_uid_cookie(resp, uid)
    return resp


@app.get("/api/jobs/{job_id}")
async def job_status(request: Request, job_id: str) -> JSONResponse:
    job = owned_job(request, job_id)
    return JSONResponse(job.to_dict(), headers={"Cache-Control": "no-store"})


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str) -> JSONResponse:
    job = owned_job(request, job_id)
    return JSONResponse({"ok": manager.cancel(job.id)})


@app.post("/api/jobs/{job_id}/retry/{index}")
async def retry_track(request: Request, job_id: str, index: int) -> JSONResponse:
    """Retry one skipped track of a finished playlist/album; merges into its ZIP."""
    job = owned_job(request, job_id)
    check_rate(request)
    try:
        manager.retry_failed_track(job, index)
    except (IndexError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True})


@app.delete("/api/jobs/{job_id}")
async def delete_job(request: Request, job_id: str) -> JSONResponse:
    job = owned_job(request, job_id)
    return JSONResponse({"ok": manager.delete(job.id)})


@app.get("/api/jobs/{job_id}/download")
async def download_file(request: Request, job_id: str) -> FileResponse:
    job = owned_job(request, job_id)
    if job.status != DONE or job.output_path is None or not job.output_path.is_file():
        raise HTTPException(status_code=409, detail="Noch nicht fertig.")
    name = job.download_name
    return FileResponse(
        job.output_path,
        media_type=job.download_type,
        headers={
            "Cache-Control": "no-store",
            # Both forms: the quoted ASCII fallback for old clients, the RFC 5987
            # form for anything with non-ASCII in the playlist title.
            "Content-Disposition": content_disposition(name),
        },
    )


def content_disposition(name: str) -> str:
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace('"', "'")
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(name, safe='')}"
    )
