from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded

from app import __version__
from app.config import get_config
from app.db import dispose_engine, init_db, session_scope
from app.logging_conf import configure_logging, get_logger
from app.ratelimit import limiter
from app.routers import (
    api_auth,
    api_channels,
    api_settings,
    api_system,
    api_vods,
    eventsub,
    hls,
    tuner,
)
from app.services.events import close_redis
from app.services.settings_store import get_settings_row
from app.worker.queue import close_pool, enqueue

log = get_logger(__name__)
cfg = get_config()
configure_logging(cfg.log_level, cfg.log_format)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg.ensure_dirs()
    await init_db()
    async with session_scope() as session:
        row = await get_settings_row(session)
        log.info(
            "jellyttv api starting",
            version=__version__,
            setup_complete=row.setup_complete,
            media_root=str(cfg.media_root),
            eventsub=row.eventsub_enabled,
        )
    # Nudge the worker so subscriptions/library state converge after a restart.
    await enqueue("reconcile_eventsub", job_id="reconcile_eventsub", defer_seconds=15)
    try:
        yield
    finally:
        await close_pool()
        await close_redis()
        await dispose_engine()
        log.info("jellyttv api stopped")


app = FastAPI(
    title="JellyTTV",
    version=__version__,
    description="Bridge Twitch livestreams and VODs into Jellyfin.",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(_request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": f"rate limit exceeded: {exc.detail}"})


app.include_router(api_auth.router)
app.include_router(api_settings.router)
app.include_router(api_channels.router)
app.include_router(api_vods.router)
app.include_router(api_system.router)
app.include_router(tuner.router)
app.include_router(hls.router)
app.include_router(eventsub.router)


# ---------------------------------------------------------------- static SPA
static_dir = cfg.static_dir
if static_dir.is_dir():
    assets = static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon():  # pragma: no cover
        path = static_dir / "favicon.svg"
        if path.exists():
            return FileResponse(path)
        return JSONResponse(status_code=404, content={"detail": "not found"})

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        # Anything that is not an API/tuner route falls through to the SPA so
        # client-side routing works on refresh.
        if full_path.startswith(("api/", "tuner/", "hls/", "eventsub/", "vod/")):
            return JSONResponse(status_code=404, content={"detail": "not found"})
        index = static_dir / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse(status_code=404, content={"detail": "ui not built"})
else:  # pragma: no cover - dev mode, Vite serves the UI

    @app.get("/", include_in_schema=False)
    async def root():
        return {
            "name": "JellyTTV",
            "version": __version__,
            "ui": "not built - run `npm run dev` in frontend/ or build the docker image",
            "docs": "/api/docs",
        }
