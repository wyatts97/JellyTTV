# Development

## Layout

```
backend/
  app/
    main.py              FastAPI app, static SPA mount
    config.py            env-driven process config (paths, redis, secret)
    db.py                async SQLite engine, create_all + additive migrations
    models.py            SQLModel tables
    schemas.py           pydantic request/response models
    security.py          admin session cookie + tuner-key guards
    crypto.py            Fernet secret encryption, argon2 password hashing
    util.py              time, filename sanitising, Twitch parsing helpers
    routers/
      api_auth.py        login / logout / setup wizard
      api_settings.py    settings, connection tests, Jellyfin libraries
      api_channels.py    channel CRUD + sync/publish/preview
      api_vods.py        VOD listing and per-VOD actions
      api_system.py      dashboard, jobs, logs, diagnostics, SSE feed
      tuner.py           M3U + XMLTV for Jellyfin's Live TV
      hls.py             HLS proxy, ad stripping, VOD redirects
      eventsub.py        Twitch webhook receiver
    services/
      twitch.py          Helix client + app-token lifecycle
      resolver.py        streamlink/yt-dlp -> playable url, with caching
      hls.py             playlist rewriting + Twitch ad detection  <-- most tested
      tuner.py           M3U / XMLTV generation
      library.py         .strm / NFO / artwork writing, pruning
      episodes.py        deterministic season/episode numbering
      channels.py        channel add/refresh, live-state transitions
      vods.py            catalogue sync, yt-dlp archiving, retention
      eventsub.py        signature verification + subscription reconciler
      jellyfin.py        Jellyfin API client
      events.py          Redis pub/sub event bus for SSE
      settings_store.py  settings singleton with decrypted accessors
    worker/
      settings.py        arq WorkerSettings, cron schedule
      tasks.py           all background jobs
      queue.py           enqueue helper shared with the API
  tests/                 pytest suite
frontend/
  src/
    App.tsx              routing + auth gating
    lib/                 api client, types, formatters, SSE hook
    components/          ui.tsx primitives, Layout
    pages/               Setup, Login, Dashboard, Channels, Vods, Jobs, Settings
docker/                  Dockerfile, entrypoint, Caddyfile
```

## Running without Docker

Needs Python 3.12+, Node 22+, Redis, and `ffmpeg` / `streamlink` / `yt-dlp` on `PATH`.

```bash
cd backend
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

export JELLYTTV_SESSION_SECRET=dev-secret
export JELLYTTV_CONFIG_DIR=../data/config
export JELLYTTV_MEDIA_ROOT=../data/media
export JELLYTTV_REDIS_URL=redis://127.0.0.1:6379/0

.venv/bin/python -m uvicorn app.main:app --reload --port 8730
```

Second terminal:

```bash
cd backend && .venv/bin/python -m arq app.worker.settings.WorkerSettings
```

Third terminal (Vite dev server proxies `/api`, `/tuner`, `/hls` to `:8730`):

```bash
cd frontend && npm install && npm run dev   # http://localhost:5173
```

> On Windows, `greenlet` and `lxml` may not have wheels for the newest Python releases. Use Python
> 3.12, or just develop against the Docker image.

## Tests

```bash
cd backend
python -m pytest -q                 # 64 tests
python -m ruff check .
python -m mypy app                  # advisory

cd ../frontend
npm run build                       # includes tsc type checking
```

The test suite deliberately concentrates on the fragile, high-value logic:

| File | Covers |
|---|---|
| `test_hls.py` | Twitch ad-segment detection and playlist rewriting |
| `test_tuner.py` | M3U ↔ XMLTV id consistency, guide window, offline handling |
| `test_library.py` | paths, NFO contents, idempotency, pruning, archive/strm modes |
| `test_twitch.py` | token caching, 401 refresh, pagination, EventSub payloads (respx) |
| `test_eventsub.py` | HMAC verification and timestamp freshness |
| `test_util_and_episodes.py` | filename sanitising, duration parsing, episode numbering |

No test makes a real network call or spawns a real subprocess.

## Adding a background job

1. Write `async def my_job(ctx, ...)` in `app/worker/tasks.py`, wrapping the body in
   `async with job_record("my_job", key=...) as job:` so it shows up in the Activity page.
2. Add it to `FUNCTIONS` in `app/worker/settings.py` (and `CRON_JOBS` if scheduled).
3. Enqueue it with `await enqueue("my_job", arg, job_id="my_job:unique")` — passing `job_id` makes
   the enqueue idempotent, which matters because several code paths trigger the same work.

## Schema changes

`init_db()` runs `create_all` plus automatic `ALTER TABLE … ADD COLUMN` for any new columns, so
adding a nullable column requires no migration step for users. Renames, type changes and drops are
**not** handled — those would need a real migration tool (Alembic) adding at that point.

## Design notes

- **Jellyfin only ever sees our URLs.** Twitch tokens expire in minutes; ours never do. This is why
  the HLS proxy and the `/vod/{id}` redirect exist rather than writing Twitch URLs into `.strm`
  files.
- **Segments are redirected, not proxied,** by default. Rewriting a few KB of playlist text is
  cheap; relaying gigabytes of video is not.
- **Episode numbers are computed, not sequential,** so two instances of JellyTTV pointed at the same
  channel produce identical numbering and nothing renumbers when an old VOD is discovered late.
- **Writes are atomic** (temp file + `os.replace`) so a Jellyfin scan never reads a half-written NFO.
- **The ad heuristic is isolated** in `services/hls.py`. Twitch will change its format; that file and
  its tests are the only things that should need touching.
