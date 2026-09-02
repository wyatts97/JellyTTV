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

## One channel works in Streamyfin but not in the Jellyfin web UI

Almost always an ad difference between the two channels, not a client difference.
A Twitch user OAuth token only yields an ad-free playlist for channels the account
is **subscribed** to (or everywhere, with Turbo); every other channel still gets
ads stitched in. Streamyfin plays the HLS URL natively and keeps retrying a
playlist, while the web UI is driven by ffmpeg on the Jellyfin server, which gives
up quickly — so a channel that hiccups during an ad break fails there first.

Confirm it by comparing a working and a failing channel while both are live:

```bash
curl -s "http://localhost:8730/api/debug/hls/FAILING?fmt=json" | jq '.upstream, .result'
```

`upstream.ad_segments > 0` with `result.ad_pod: true` means the channel is in a
break. Then time the endpoint itself — it must answer promptly even mid-break:

```bash
curl -s -o /dev/null -w '%{http_code} %{time_total}
'   "http://localhost:8730/hls/FAILING/master.m3u8?key=YOUR_KEY"
```

Anything above a couple of seconds is a bug: the backup-stream search runs
detached from the request and `PLAYLIST_DEADLINE_SECONDS` caps the handler. Check
`GET /api/debug/hls/sessions` for `backup_searching` stuck true or a large
`backup_last_attempt_s`.

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
| `AD[daterange]` on most/all segments | Ad detection is over-matching. If stripping would empty the playlist, JellyTTV passes the pod through instead and logs `ad pod with no backup - passing it through` — you get ads, not a hole. |
| `low latency: True` with `dropped=` | Twitch sent LL-HLS tags. These are dropped deliberately; their URIs are relative to the upstream host and would break any client that followed them. |
| `session` → `media_sequence` | Must only ever increase. Poll a few times; if it moves backwards, that is a bug worth reporting. |
| `upstream mseq` vs session `media_sequence` | They are unrelated by design. JellyTTV assigns its own sequence numbers so ad removal and weaver switches stay invisible to the player. |

`GET /api/debug/hls/sessions` lists every active session with its sequence
bookkeeping and how many times it has had to re-resolve. A steadily climbing
`resolves` count means Twitch keeps dropping the upstream.

Add `?refresh=1` to force a fresh streamlink resolve and start a new session.

## Audio drifts out of sync with video

Specifically: a few stutters or freezes, then the picture returns with audio
seconds behind — and the gap grows over a session rather than recovering.

The delay is not arbitrary. **It is the amount of content removed from the
timeline.** ffmpeg's HLS demuxer does not implement `#EXT-X-DISCONTINUITY`
([ffmpeg trac #5419](https://trac.ffmpeg.org/ticket/5419)), and Jellyfin's web
client cannot avoid that demuxer — a live HLS source is not in jellyfin-web's
DirectPlayProfiles, so playback falls back to server-side ffmpeg even though
hls.js would have handled the discontinuity. So any hole cut in the stream
arrives as a raw timestamp jump, which video and audio absorb differently.

JellyTTV therefore never cuts a hole. When an ad break cannot be covered by a
clean backup stream, **the ad plays** — a continuous stream with an ad in it
beats a stream that drifts apart permanently. That is deliberate, not a
regression, and it is why you may see more ads than before.

If you still see drift:

- Confirm it from Jellyfin's side. Its transcode log (`/var/log/jellyfin/
  ffmpeg-transcode-*.log`, or Dashboard → Logs) shows the exact ffmpeg command
  and any `Non-monotonous DTS` / timestamp-jump warnings. **A reported jump
  close to the length of an ad break is this bug**; no warnings means the cause
  is elsewhere.
- `GET /api/debug/hls/sessions` — `ad_pod_polls` should stay low. A large count
  means breaks are being detected constantly, which is worth reporting.
- Check for `timeline gap detected from program-date-time` in the API log. That
  is JellyTTV noticing upstream itself skipped time; it marks the seam, but
  ffmpeg will still ignore the marker. Frequent occurrences point at a flaky
  connection to Twitch rather than at ad handling.
- If drift persists across all of that, enable **Settings → Re-encode to a
  continuous output**. It runs a dedicated encoder per channel and discards
  upstream timestamps entirely, which makes the problem impossible by
  construction — at the cost of roughly 1.5–3 CPU cores per 1080p60 channel in
  software. Pick a hardware encoder if your host has one.

## The stream freezes during ad breaks with re-encoding on

Expected when nothing can cover the break. Re-encoding fixes *timing*, not
missing content: while an ad plays on our token, Twitch is not sending the
broadcaster's video at all, so there is nothing to encode. What you get is a
clean freeze and an in-sync resume, rather than a desync.

Coverage comes from the backup search, not the encoder. `stats.backup_polls` in
`GET /api/debug/hls/sessions` is how often a break was actually covered — if it
stays at zero, the encoder is costing you CPU for nothing and should go back
off. A Turbo subscription or a per-channel sub is the only thing that removes
ads upstream and so avoids the question entirely.

## Ads still play

- Ad stripping only works with **Settings → Proxy playlists through JellyTTV** enabled.
- A break that no clean backup could cover plays the ad on purpose. See
  "Audio drifts out of sync with video" for why cutting it is worse.
- Twitch changes its ad-stitching format periodically. Set `JELLYTTV_LOG_LEVEL=DEBUG` and look for
  `stripped twitch ad segments`. If the count is always 0 during an ad break, the detection heuristic
  needs updating — it lives in one small file, `backend/app/services/hls.py`, with tests in
  `backend/tests/test_hls.py`.
- Adding a **user OAuth token** (Settings → Twitch) significantly reduces the ads Twitch serves in
  the first place, and unlocks 1440p/H.265.

## The guide is stale — live/offline changes take ages to appear

Jellyfin caches the downloaded XMLTV file on disk for **one hour**, at
`<cache>/xmltv/<listings-provider-id>.xml`, keyed by the provider's id and expired
purely by file age. No `Cache-Control` header we send affects it, and running
Jellyfin's own "Refresh Guide" task inside that hour just re-parses the stale
copy. That, on its own, is a guide that is up to an hour behind at an arbitrary
phase.

JellyTTV works around it by recreating the XMLTV listings provider, which changes
the id and therefore the cache filename — see `JellyfinClient.force_guide_refresh`.
It is controlled by **Force guide updates through Jellyfin's cache** in Settings
(on by default) and rate-limited to once every few minutes, because each recreate
leaves the previous `<guid>.xml` behind in Jellyfin's cache directory.

If the guide still looks stale:

- Check `GET /api/jobs` for `jellyfin_refresh_guide`. `guide refresh triggered
  (cache bypassed)` is the forced path; plain `guide refresh triggered` means it
  fell back — usually because no listings provider matches `self_base_url` +
  `/tuner/guide.xml`. Make sure **Base URL for Jellyfin** matches the URL you
  actually entered as the XMLTV provider in Jellyfin.
- `could not reach Jellyfin` / `rejected the API key` means the API key is not an
  admin key; recreating a listings provider requires elevation.
- Offline channels deliberately have **no programmes at all**, so Jellyfin's
  "On Now" cannot show them; they still appear under Channels. A blank guide row
  for a channel that is not streaming is working as intended. If you do see
  "Offline" cards in On Now, Jellyfin is serving guide data from before this
  behaviour changed - force a refresh and they will clear.

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
