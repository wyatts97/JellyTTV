# JellyTTV

<p align="center">
  <img src="backend/app/icons/icon-512.png" width="128" height="128" alt="JellyTTV" />
</p>

Self-hosted Twitch companion: watch your channels ad-free in its own web app, get go-live push
notifications, and bridge everything into Jellyfin — live streams as **Live TV channels** (with a
real EPG), and past broadcasts as **episodes** of a per-channel series.

- **Built-in player** — watch any tracked channel right in the dashboard (**Watch** in the menu,
  or click a live card). The browser player handles what Jellyfin's ffmpeg can't, so it plays at
  **full quality** and only switches to Twitch's never-ad-stitched 360p source for the length of an
  ad break, then switches back. You can also lock it to *360p ad-free* from the player. It includes
  Twitch chat, picture-in-picture, keyboard shortcuts, and automatic recovery from stalls.
- **Installable app with go-live alerts** — JellyTTV is a PWA. Install it to your phone or
  desktop, turn on notifications, and it pushes to every subscribed device when a channel goes
  live; tapping the notification opens the stream. No Jellyfin client or plugin needed.
- **Live TV** — JellyTTV serves a dynamic M3U playlist and XMLTV guide. Point Jellyfin's *M3U
  Tuner* at it and every tracked channel becomes a Live TV channel showing the current title,
  category and viewer count in the guide.
- **No ads in Jellyfin either** — every Jellyfin channel is served from `picture-by-picture`, the one Twitch
  player type that is never ad-stitched, so a break is simply not there: no ad, no black screen,
  no frozen picture. That player type tops out at **360p**, which is the deliberate trade. Turn
  *Always use the ad-free source* off to watch at full quality and have breaks covered by
  switching sources instead.
- **VODs as episodes** — each channel becomes a Jellyfin *Series*; each broadcast becomes an
  episode with proper NFO metadata, artwork and stable `SxxExxxx` numbering. Choose per channel
  between zero-storage `.strm` links or full yt-dlp archiving with retention rules.
- **Instant go-live** — Twitch EventSub webhooks when you have public HTTPS, automatic polling
  fallback when you don't.
- **Streamyfin notifications (optional)** — go-live pushes can also go through the
  [Streamyfin companion plugin](https://github.com/streamyfin/jellyfin-plugin-streamyfin) using the
  Jellyfin API key you already configured. (Jellyfin's own web app cannot receive push notifications
  — its service worker only does offline caching — so a client that supports them is required.) The
  plugin is third-party and must itself be built for Jellyfin 12.0; until it is, JellyTTV treats it
  as simply not installed and everything else keeps working.
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
| Jellyfin **12.0 or newer** (optional) | Only for Live TV and the VOD library — the built-in player needs no Jellyfin. [jellyfin.org/downloads](https://jellyfin.org/downloads); 10.11 and earlier are not supported |
| Twitch Client ID + Secret | [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps) — client type **Confidential** |
| Jellyfin API key (optional) | Jellyfin → Dashboard → API Keys |
| Public HTTPS URL (optional) | For EventSub webhooks and for push notifications (browsers only allow Web Push over HTTPS). The bundled Caddy profile provides one |

Using Jellyfin? Then read **[docs/jellyfin-setup.md](docs/jellyfin-setup.md)** — there are three Jellyfin settings
that must be right or your library will look wrong.

---

## How it works

```
Twitch Helix + EventSub
          │
          ▼
┌──────────────────────────────┐
│           api                │  FastAPI: dashboard, tuner, live stream, webhooks
│  /api  /tuner  /stream  /vod │
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
Jellyfin ──play──► /stream/{login}.ts        → one continuous MPEG-TS stream from streamlink
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

## Watching in the browser

Open **Watch** in the sidebar, or click any live card on the Dashboard. The player gets its
playlist from JellyTTV, which detects ads and switches to a clean copy of the stream. JellyTTV asks
Twitch for playback tokens the same way Twitch's own player does, so finding that clean copy takes
well under a second and starting a stream no longer waits on a `streamlink` launch. The browser fetches the video itself straight from Twitch's CDN, so watching
costs your server almost no bandwidth. Turn on *Settings → Built-in player → Stream video through
JellyTTV* only if the viewing device can't reach Twitch.

| Player source | What you get |
|---|---|
| **Best** (default) | The channel's full quality. During an ad break the player switches to a clean copy of the same stream — at the same resolution and frame rate wherever one exists, so the picture does not change — and switches back when the break ends. A green *Ad break blocked* badge shows while this is happening. |
| **360p ad-free** | `picture-by-picture` from the start. Capped at 360p, with nothing to switch. |

Shortcuts: <kbd>Space</kbd>/<kbd>K</kbd> play/pause, <kbd>M</kbd> mute, <kbd>F</kbd> fullscreen,
<kbd>L</kbd> jump to live. Chat is Twitch's own embed; sign in to twitch.tv in the same browser to
send messages.

### Install the app and get go-live notifications

1. Open JellyTTV over **HTTPS** (for example with `docker compose --profile with-caddy up -d`).
   Browsers refuse Web Push on plain `http://`, except on `localhost`.
2. Install it: *Install app* in Settings, or the browser's install button. On **iPhone/iPad**,
   tap *Share → Add to Home Screen*, then open JellyTTV from the home screen, because iOS only
   allows push inside an installed app.
3. *Settings → Go-live notifications → Enable notifications*, once on each device. *Send test*
   confirms delivery. Mute individual channels with the bell on the Channels page.

Notifications are sent by JellyTTV itself (standard Web Push with a key generated for your
install). They don't depend on Jellyfin, and the optional Streamyfin route runs independently.

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

Ad blocking follows the technique worked out by
**[TTV AB](https://github.com/GosuDRM/TTV-AB)** by GosuDRM — the backup-stream strategy
(`backend/app/services/adblock.py`), its fast low-quality bridge and hold segment
(`backend/app/services/stream_session.py`), and the ad-progress signalling
(`backend/app/services/ad_events.py`). Twitch stitches ads per playback token, so the same
channel requested for a different player type is usually not in the same break; playing that
during a break is what keeps the picture moving instead of stopping. Used under its MIT-based
licence with attribution, and reimplemented from observed behaviour — no source was copied.

Three further refinements come from
**[Alternate Player for Twitch.tv](https://addons.mozilla.org/firefox/addon/twitch_5/)** by
Alexander Choporov (CoolCmd), BSD-3-Clause: the `picture-by-picture` player type, which is the one
that extension still mints its ad-free playlist with and which now leads the backup rotation
(`backend/app/services/adblock.py`); starting the backup search when a pod first appears at the
live edge rather than once it fills the window, so the break is covered a poll sooner
(`backend/app/services/stream_session.py`); and the shape of the replayed ad telemetry
(`backend/app/services/ad_events.py`). Reimplemented from observed behaviour — no source was
copied.

## Licence

MIT
