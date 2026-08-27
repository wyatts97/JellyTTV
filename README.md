# JellyTTV

<p align="center">
  <img src="backend/app/icons/icon-512.png" width="128" height="128" alt="JellyTTV" />
</p>

Self-hosted bridge that makes Twitch channels appear inside Jellyfin — live streams as **Live TV
channels** (with a real EPG), and past broadcasts as **episodes** of a per-channel series.

- **Live TV** — JellyTTV serves a dynamic M3U playlist and XMLTV guide. Point Jellyfin's *M3U
  Tuner* at it and every tracked channel becomes a Live TV channel showing the current title,
  category and viewer count in the guide.
- **Ad stripping** — playlists are proxied and Twitch's stitched ad segments are removed before
  Jellyfin ever sees them.
- **VODs as episodes** — each channel becomes a Jellyfin *Series*; each broadcast becomes an
  episode with proper NFO metadata, artwork and stable `SxxExxxx` numbering. Choose per channel
  between zero-storage `.strm` links or full yt-dlp archiving with retention rules.
- **Instant go-live** — Twitch EventSub webhooks when you have public HTTPS, automatic polling
  fallback when you don't.
- **Go-live push notifications** — optional push to your phone or tablet when a tracked channel
  starts streaming, delivered through the
  [Streamyfin companion plugin](https://github.com/streamyfin/jellyfin-plugin-streamyfin) using the
  Jellyfin API key you already configured. (Jellyfin's own web app cannot receive push notifications
  — its service worker only does offline caching — so a client that supports them is required.)
- **One-command install** — Docker Compose, a setup wizard, and a React dashboard for everything.

> **Use responsibly.** JellyTTV is for personal use with content you are entitled to access. You
> are responsible for complying with the [Twitch Terms of Service](https://www.twitch.tv/p/legal/terms-of-service/)
> and Developer Agreement.

---

## Quick start

```bash
git clone https://github.com/wyatts97/jellyttv.git
cd jellyttv
cp .env.example .env

# Generate a session secret and paste it into .env as JELLYTTV_SESSION_SECRET
python -c "import secrets; print(secrets.token_urlsafe(48))"

docker compose up -d
```

Open **http://localhost:8730** and follow the five-step setup wizard.

You will need:

| Requirement | Where to get it |
|---|---|
| Twitch Client ID + Secret | [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps) — client type **Confidential** |
| Jellyfin API key (optional) | Jellyfin → Dashboard → API Keys |
| Public HTTPS URL (optional) | Only for EventSub webhooks; see [reverse proxy](docs/reverse-proxy.md) |

Then read **[docs/jellyfin-setup.md](docs/jellyfin-setup.md)** — there are three Jellyfin settings
that must be right or your library will look wrong.

---

## How it works

```
Twitch Helix + EventSub
          │
          ▼
┌──────────────────────────────┐
│           api                │  FastAPI: dashboard, tuner, HLS proxy, webhooks
│  /api  /tuner  /hls  /vod    │
└───┬──────────┬───────────┬───┘
    │          │           │
 SQLite    Redis+arq    writes .strm/.nfo/artwork
              │              │
              ▼              ▼
           worker      /media/twitch ──► mounted into Jellyfin as a "Shows" library
    (sync, downloads,
     retention, EventSub)

Jellyfin ──M3U──►  /tuner/playlist.m3u     dynamic, live status per channel
Jellyfin ──XMLTV─► /tuner/guide.xml        now/next programme data
Jellyfin ──play──► /hls/{login}/master.m3u8 → ads stripped → segments redirected to Twitch CDN
Jellyfin ──play──► /vod/{video_id}         → 302 to a freshly resolved VOD url
```

Because Jellyfin only ever talks to *our* stable URLs, short-lived Twitch tokens are re-resolved
transparently and playback never breaks mid-session. The same applies to `.strm` files: they point
at `/vod/{id}`, so archived episodes keep working after Twitch's signed URLs expire.

### On-disk layout

```
/media/twitch/
└── Example Streamer/
    ├── tvshow.nfo   poster.jpg   fanart.jpg
    └── Season 2026/
        ├── season.nfo
        ├── Example Streamer - S2026E0630 - Ranked grind.strm
        ├── Example Streamer - S2026E0630 - Ranked grind.nfo
        └── Example Streamer - S2026E0630 - Ranked grind-thumb.jpg
```

Episode numbers are deterministic — season is the broadcast year, episode is
`day_of_year × 10 + index_within_day` — so they sort chronologically and never get renumbered.

---

## Configuration

Paths, the Redis URL and the session secret come from the environment (see
[`.env.example`](.env.example)). **Everything else is configured in the web UI** and stored in
SQLite, with credentials encrypted at rest.

| Environment variable | Default | Purpose |
|---|---|---|
| `JELLYTTV_PORT` | `8730` | Published HTTP port |
| `MEDIA_ROOT` | `./data/media` | Library tree; mount this into Jellyfin |
| `CONFIG_ROOT` | `./data/config` | SQLite DB + encryption key |
| `JELLYTTV_SESSION_SECRET` | — | **Required.** Signs admin session cookies |
| `JELLYTTV_PUBLIC_BASE_URL` | empty | Public HTTPS URL; enables EventSub webhooks |
| `PUID` / `PGID` | `1000` | Run as this uid/gid so Jellyfin can read the files |
| `JELLYTTV_LOG_LEVEL` | `INFO` | `DEBUG` for verbose resolver/proxy logs |

Optional compose profiles:

```bash
docker compose --profile with-caddy up -d      # auto-HTTPS reverse proxy (enables EventSub)
docker compose --profile with-jellyfin up -d   # all-in-one demo, brings its own Jellyfin
```

---

## Jellyfin Plugin (optional)

JellyTTV ships with an optional companion plugin that adds Twitch live streams directly to
Jellyfin's sidebar navigation and home screen — with live thumbnails, viewer counts, and
go-live notifications. No more digging through Live TV to see who's streaming.

### Install from the Jellyfin catalog

1. Open **Jellyfin → Dashboard → Plugins → Repositories**
2. Click **+** and add:
   ```
   https://raw.githubusercontent.com/wyatts97/JellyTTV/main/jellyfin-plugin-jellyttv/manifest.json
   ```
3. Go to the **Catalog** tab, search for **JellyTTV**, and install it
4. Restart Jellyfin
5. Open **Dashboard → Plugins → JellyTTV** and enter your JellyTTV backend URL
   (e.g. `http://jellyttv-api:8730`)
6. Hard-refresh your browser (Ctrl+Shift+R)

You'll now see a **Twitch** link in the sidebar and a **Live on Twitch** section on your
home screen showing all currently live streamers.

### Build from source

```bash
cd jellyfin-plugin-jellyttv
dotnet publish -c Release -o bin/publish
```

Then copy `bin/publish/*` into Jellyfin's plugin directory
(`~/.local/share/jellyfin/plugins/JellyTTV/` on Linux) and restart.

---

## Documentation

- **[Jellyfin setup](docs/jellyfin-setup.md)** — tuner, guide, and the library settings that matter
- **[Reverse proxy & EventSub](docs/reverse-proxy.md)** — Caddy, Traefik, Cloudflare Tunnel, nginx
- **[Troubleshooting](docs/troubleshooting.md)** — nothing plays, no episodes, wrong metadata…
- **[Development](docs/development.md)** — run it without Docker, tests, project layout

---

## Development

```bash
# Backend
cd backend
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m uvicorn app.main:app --reload --port 8730
.venv/bin/python -m arq app.worker.settings.WorkerSettings   # separate terminal, needs Redis

# Frontend (proxies /api to :8730)
cd frontend && npm install && npm run dev
```

```bash
cd backend && python -m pytest -q && python -m ruff check .
cd frontend && npm run build
```

Requires Python 3.12+, Node 22+, Redis, plus `ffmpeg`, `streamlink` and `yt-dlp` on `PATH`.

## Credits

Ad blocking follows techniques worked out by two browser extensions:

- **[TTV AB](https://github.com/GosuDRM/TTV-AB)** by GosuDRM — the backup-stream strategy
  (`backend/app/services/adblock.py`) and the ad-progress signalling
  (`backend/app/services/ad_events.py`). Twitch stitches ads per playback token, so the same
  channel requested for a different player type is usually not in the same break; playing that
  during a break is what keeps the picture moving instead of stopping. Used under its MIT-based
  licence with attribution.
- **[TTV LOL PRO](https://github.com/younesaassila/ttv-lol-pro)** by Younes Aassila — the
  ad-free-region proxy strategy, selectable as an alternative.

Both techniques were reimplemented from observed behaviour; no source was copied.

## Licence

MIT
