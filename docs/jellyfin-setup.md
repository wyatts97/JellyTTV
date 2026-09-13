# Jellyfin setup

Three things to configure. The third one is the one people get wrong.

Grab the exact URLs (including the tuner key) from **JellyTTV → Settings → Add to Jellyfin**.

> **Jellyfin 12.0 or newer is required.** JellyTTV relies on 12.0 clearing the guide cache when a
> listings provider is saved, which is what keeps live/offline state current. It also authenticates
> with the `Authorization: MediaBrowser` scheme — the one 12.0 kept when it disabled its legacy
> mechanisms by default. Upgrading an existing server from 10.11 is a one-way trip: back up the data
> directory first, and run a full library scan afterwards.

---

## 1. Live TV tuner

**Jellyfin → Dashboard → Live TV → Tuner Devices → `+`**

| Field | Value |
|---|---|
| Tuner Type | **M3U Tuner** |
| File or URL | `http://jellyttv:8730/tuner/playlist.m3u?key=YOUR_KEY` |
| Simultaneous stream limit | **Leave blank** (see below) |

The URL must be reachable **from the Jellyfin server**. If Jellyfin and JellyTTV are in the same
compose project use `http://api:8730`; otherwise use the LAN IP of the JellyTTV host. Never
`localhost` — that resolves inside the Jellyfin container.

Set this value in JellyTTV under **Settings → JellyTTV base URL**, because it is also baked into
every `.strm` file.

**Why the stream limit should stay blank.** Each channel is delivered as one continuous MPEG-TS
stream (`/stream/{login}.ts`). Jellyfin 12 can *direct play* that — hand it to the client without
re-muxing — but it switches direct play off for any M3U tuner that has a simultaneous stream limit
set ([`M3UTunerHost`](https://github.com/jellyfin/jellyfin/blob/v12.0/src/Jellyfin.LiveTv/TunerHosts/M3UTunerHost.cs)).
Re-muxing a live source is the path with Jellyfin's open timestamp/judder bug
[#17788](https://github.com/jellyfin/jellyfin/issues/17788), so leave the limit blank and cap
concurrent streams in JellyTTV instead, with `JELLYTTV_MAX_LIVE_STREAMS` (default 8).

**Upgrading from a version that served `.m3u8` tuner urls.** Jellyfin keeps each channel's url in
memory. After upgrading, run **Dashboard → Scheduled Tasks → Refresh Guide** (or restart Jellyfin)
so it picks up the new `.ts` urls. Channel ids come from `tvg-id`, not the url, so your channels and
favourites are kept. If a channel then fails on its first play, delete Jellyfin's
`cache/mediainfo/` directory: it caches probe results per stream and may still hold the old HLS ones.

## 2. Guide data

**Jellyfin → Dashboard → Live TV → TV Guide Data Providers → `+` → XMLTV**

```
http://jellyttv:8730/tuner/guide.xml?key=YOUR_KEY
```

Jellyfin allows only **one** guide provider at a time (XMLTV *or* Schedules Direct). JellyTTV emits
`tvg-id="twitch.<login>"` in the playlist and the identical `<channel id="…">` in the XMLTV, so
channel mapping is automatic. If Jellyfin still shows unmapped channels, open the `…` menu next to
the provider and choose **Map Channels**.

Then run **Dashboard → Scheduled Tasks → Refresh Guide** once. After that JellyTTV keeps the guide
current on its own, including past Jellyfin's one-hour guide cache — see
[troubleshooting.md](troubleshooting.md).

Live programmes carry an XMLTV `<live/>` tag, which Jellyfin 12.0 reads into `IsLive` and surfaces as
a **LIVE** badge in the guide. Earlier versions ignored it.

Offline channels stay in the playlist by design, with a `<channel>` entry but no programmes, so they
never appear in *On Now*. Jellyfin keys channels by id, so adding and removing them churns its
database and loses user favourites. You can change this in **JellyTTV → Settings → Keep offline
channels in the tuner**.

## 3. The Shows library — read this one

**Jellyfin → Dashboard → Libraries → Add Media Library**

| Setting | Value |
|---|---|
| Content type | **Shows** |
| Folder | the path where you mounted `MEDIA_ROOT` (e.g. `/media/twitch`) |
| **Enable real-time monitoring** | On — new episodes appear without waiting for a scan |

Then, critically:

- ✅ **Enable** *"NFO"* under **Metadata savers / readers**.
- ❌ **Disable every internet metadata provider** (TheTVDB, TMDb, OMDb, …) for this library.
- ❌ **Disable every image provider** for this library.

Why: your Twitch channels are not real TV shows. Left enabled, Jellyfin will confidently match
"Example Streamer" to some unrelated series and replace the titles, plots and artwork that JellyTTV
wrote. JellyTTV writes complete NFO files and artwork, including `<lockdata>true</lockdata>`, but
disabling the providers is the reliable fix.

---

## Verifying it works

```bash
# Playlist should list your channels
curl -s "http://localhost:8730/tuner/playlist.m3u?key=YOUR_KEY" | head

# Guide should contain <programme> entries
curl -s "http://localhost:8730/tuner/guide.xml?key=YOUR_KEY" | head -30

# A live channel should stream MPEG-TS: every packet starts with the byte 0x47 ("G")
curl -s "http://localhost:8730/stream/SOMELOGIN.ts?key=YOUR_KEY" | head -c 188 | head -c 1

# And should actually play
ffplay "http://localhost:8730/stream/SOMELOGIN.ts?key=YOUR_KEY"
```

In Jellyfin: **Live TV → Channels** should list your channels; **Guide** should show the current
stream title for anyone who is live.

---

## A note on `.strm` episodes

Jellyfin deliberately does **not** run `ffprobe` on remote `.strm` files during a library scan. This
means:

- Episode **duration** comes from the `<runtime>` tag JellyTTV writes into the NFO. Nothing else can
  supply it until the file has been played once.
- **Codec / resolution details are empty** until first playback. This is normal.
- Resume/"next up" behaviour is less reliable than for real files.

If any of that bothers you, switch the channel to **Archive to disk** mode — then real `.mp4` files
are produced and Jellyfin treats them like any other media.

## Tuner key

Both tuner URLs are protected by a key so that a random device on your LAN cannot enumerate and
proxy streams through your server. If you rotate it (**Settings → Rotate key**), you must update
both URLs in Jellyfin — JellyTTV automatically rewrites all `.strm` files for you.
