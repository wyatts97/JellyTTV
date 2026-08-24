# Troubleshooting

Start with **Settings → Diagnostics** in the UI — it shows binary versions, whether the media root
is writable, free disk space and EventSub health. Then **Activity** shows every job and its result.

```bash
docker compose logs -f api worker
```

---

## Channels do not appear in Jellyfin's Live TV

1. Fetch the playlist yourself:
   ```bash
   curl -s "http://localhost:8730/tuner/playlist.m3u?key=YOUR_KEY"
   ```
   - Empty apart from `#EXTM3U`? The channels are disabled, or **Show in Live TV guide** is off.
   - `403 Invalid or missing tuner key`? The `?key=` in Jellyfin is stale — copy it again from
     Settings.
2. Check the URL is reachable **from the Jellyfin container**, not just your browser:
   ```bash
   docker compose exec jellyfin curl -sI "http://api:8730/api/health"
   ```
   `localhost` in the tuner URL is the single most common mistake.
3. Re-save the tuner in Jellyfin to force a channel refresh.

## A channel plays nothing / errors immediately

```bash
curl -sv "http://localhost:8730/hls/SOMELOGIN/master.m3u8?key=YOUR_KEY"
```

| Response | Meaning |
|---|---|
| `503 … is offline` | The channel genuinely is not live. |
| `502 could not resolve stream` | streamlink and yt-dlp both failed — see below. |
| `404 channel … is not tracked` | Login mismatch; the channel was renamed on Twitch. |
| A playlist of `.ts` URLs | JellyTTV is fine; the problem is in Jellyfin/ffmpeg. |

For resolver failures, test the tooling directly:

```bash
docker compose exec api streamlink --stream-url https://www.twitch.tv/SOMELOGIN best
docker compose exec api yt-dlp -g https://www.twitch.tv/SOMELOGIN
```

Common causes: outdated streamlink/yt-dlp after a Twitch backend change (`docker compose pull &&
docker compose up -d`), subscriber-only content (add a user OAuth token in Settings), or the host
being geo/IP-blocked by Twitch.

## A channel stutters, pauses, or dies after a few minutes

Almost always a playlist-continuity problem, and it only shows up on channels
that actually run stitched ads or get moved between Twitch's `video-weaver`
nodes — which is why one channel can be perfect while another is unwatchable.

Start with the HLS debug endpoint (admin session required, not the tuner key):

```bash
curl -s "http://localhost:8730/api/debug/hls/SOMELOGIN?fmt=text"
```

It prints the raw upstream playlist and the playlist JellyTTV hands to Jellyfin
side by side, plus per-segment detail. What to look for:

| Field | Meaning |
|---|---|
| `AD[daterange]` on most/all segments | Ad detection is over-matching. If the playlist would be emptied, JellyTTV passes it through instead and logs `ad strip would empty the playlist` — you get ads, not a crash. |
| `low latency: True` with `dropped=` | Twitch sent LL-HLS tags. These are dropped deliberately; their URIs are relative to the upstream host and would break any client that followed them. |
| `session` → `media_sequence` | Must only ever increase. Poll a few times; if it moves backwards, that is a bug worth reporting. |
| `upstream mseq` vs session `media_sequence` | They are unrelated by design. JellyTTV assigns its own sequence numbers so ad removal and weaver switches stay invisible to the player. |

`GET /api/debug/hls/sessions` lists every active session with its sequence
bookkeeping and how many times it has had to re-resolve. A steadily climbing
`resolves` count means Twitch keeps dropping the upstream.

Add `?refresh=1` to force a fresh streamlink resolve and start a new session.

## Ads still play

- Ad stripping only works with **Settings → Proxy playlists through JellyTTV** enabled.
- Twitch changes its ad-stitching format periodically. Set `JELLYTTV_LOG_LEVEL=DEBUG` and look for
  `stripped twitch ad segments`. If the count is always 0 during an ad break, the detection heuristic
  needs updating — it lives in one small file, `backend/app/services/hls.py`, with tests in
  `backend/tests/test_hls.py`.
- Adding a **user OAuth token** (Settings → Twitch) significantly reduces the ads Twitch serves in
  the first place, and unlocks 1440p/H.265.

## No episodes show up

1. **Activity** → is there a `vod_sync` job? What does its result say?
2. Force one: **Channels → Sync VODs**.
3. Is the channel's VOD mode set to `No VODs`?
4. Does Twitch actually have VODs? Broadcasters must enable *Store past broadcasts*, and they expire
   after 7–60 days depending on the account.
5. Check the files exist on disk:
   ```bash
   docker compose exec api find /media/twitch -maxdepth 3
   ```
6. If files exist but Jellyfin does not show them: **Settings → Scan now**, and confirm the library
   folder is the same path you mounted.

VODs are synced 15 minutes after a stream ends (Twitch needs time to publish them) and every 6 hours.

## Wrong titles, posters or plots on episodes

Internet metadata providers are enabled on the library. See
[jellyfin-setup.md § 3](jellyfin-setup.md#3-the-shows-library--read-this-one). After disabling them,
select the library → `…` → **Refresh metadata** → *Replace all metadata*.

## Episodes have no duration / no video info

Expected for `.strm` items — Jellyfin skips `ffprobe` on remote files during a scan. Duration comes
from the `<runtime>` NFO tag JellyTTV writes; codec details populate after first playback. Switch the
channel to **Archive to disk** if you want fully probed files.

## Downloads fail or stall

- `only X GiB free, need at least 5.0 GiB` — free space, or lower retention limits.
- Check the error text on the VOD row in the **VODs** page; it is the tail of yt-dlp's output.
- Downloads resume (`--continue`), so **Retry** on a failed row is cheap.
- Two downloads run at once by default. A 1080p Twitch stream is roughly 2–8 GB per hour.

## EventSub is not working

- Badge shows *Polling mode*? Either **Use EventSub webhooks** is off or the public URL is not
  `https://`.
- Subscriptions stuck at `webhook_callback_verification_pending`: Twitch cannot reach your callback.
  Verify from **outside** your network:
  ```bash
  curl -sI https://jellyttv.example.com/api/health
  ```
  Also make sure `/eventsub/*` is not behind an auth proxy (Cloudflare Access, basic auth, …).
- Signature failures in the logs mean the stored secret and Twitch's copy diverged. Click
  **Reconcile** to recreate the subscriptions.

## Everything is slow / high CPU

Turn **off** *Proxy video segments*. When off (the default), JellyTTV only rewrites tiny text
playlists and redirects the actual video to Twitch's CDN. When on, all video flows through the
container.

## Reset

```bash
docker compose down
rm -rf ./data/config          # database + encryption key -> setup wizard runs again
rm -rf ./data/media           # generated library (deletes archived VODs!)
docker compose up -d
```

To keep credentials working across a move, copy **both** `data/config/jellyttv.db` and
`data/config/secret.key`.
