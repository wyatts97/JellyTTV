# The optional Jellyfin plugin

**Status: design, not built.** This document exists so the work is understood
before it starts, and so the limitation it removes is written down rather than
rediscovered.

## The problem it solves

JellyTTV integrates with Jellyfin through the built-in **M3U tuner host**:
`backend/app/routers/tuner.py` serves `playlist.m3u` and `guide.xml`, and
Jellyfin is pointed at them as an ordinary IPTV source.

That path has one consequence that shapes everything downstream. A live HLS
source is not in jellyfin-web's `DirectPlayProfiles`, so the web client cannot
hand the URL to hls.js — playback falls back to **server-side ffmpeg**, whose
HLS demuxer [does not implement `#EXT-X-DISCONTINUITY`](https://trac.ffmpeg.org/ticket/5419)
and cannot follow a mid-stream format change.

Every ad-break freeze, black screen and "it won't play in the web UI" report in
this repository's history traces back to that. And ad avoidance necessarily
produces exactly the things that demuxer cannot absorb:

| What JellyTTV does at a break | What the demuxer does with it |
|---|---|
| splices a clean backup stream (different weaver node, own timestamp base) | ignores the discontinuity, carries the jump through |
| accepts a lower-resolution backup to cover the break faster | keeps its old decoder context; picture freezes |
| holds on a 640×360 black segment while searching | same format change, same freeze |

The re-encoding normaliser used to paper over this by transcoding every source
into one fixed shape. It cost 1.5–3 CPU cores per 1080p60 channel to fix a
problem that only exists because of the transport, and it has been removed.

hls.js — which the browser would use if it were allowed to — handles all three
correctly. So does AVPlayer on iOS, ExoPlayer on Android, and the player in most
TV clients. **Native clients are already fine today.** This plugin is about the
web client.

## The design

A small C# plugin implementing `ITunerHost`, modelled on Jellyfin's own
`M3UTunerHost` and registered through `IPluginServiceRegistrator`. Rather than
Jellyfin seeing an opaque M3U URL, the plugin talks to JellyTTV's API and returns
a `MediaSourceInfo` per channel that asks for direct play:

```csharp
new MediaSourceInfo
{
    Protocol             = MediaProtocol.Http,
    Path                 = $"{baseUrl}/hls/{login}/master.m3u8?key={tunerToken}",
    Container            = "hls",
    IsInfiniteStream     = true,
    RequiresOpening      = false,
    RequiresClosing      = false,
    SupportsDirectPlay   = true,
    SupportsDirectStream = true,
    SupportsTranscoding  = true,   // clients that genuinely cannot play HLS
    IgnoreDts            = true,
    IgnoreIndex          = true,
    AnalyzeDurationMs    = 3000,   // do not misjudge a stream that changes shape
}
```

`SupportsTranscoding` stays true on purpose. The goal is to stop *forcing* every
client through ffmpeg, not to break the ones that need it.

### The part that needs verifying first

Whether jellyfin-web actually takes the direct-play path depends on its device
profile advertising `application/x-mpegURL`, and on how `MediaSourceInfo` is
scored for live TV in the target Jellyfin version. **Verify this against the
Jellyfin version you are targeting before writing the plugin** — build a minimal
tuner host that returns the above, point the web client at it, and confirm from
Dashboard → Logs that no transcode process starts.

If the profile blocks it, the fallback is the older approach: inject a client
script that forces hls.js for JellyTTV URLs. Note that this repository already
tried DOM and script injection in the now-abandoned `jellyfin-plugin-jellyttv`,
and it broke repeatedly across Jellyfin versions. Treat it as the fallback, not
the plan.

### Second win, nearly free

`JellyfinClient.force_guide_refresh` currently deletes and recreates the XMLTV
listings provider for no reason except to change its id, because Jellyfin caches
the guide on disk at `<cache>/xmltv/<provider-id>.xml` and expires it purely by
file age — one hour, at an arbitrary phase. A plugin running in-process can
refresh the guide directly, and that hack goes away.

## Explicitly out of scope

Reviving `jellyfin-plugin-jellyttv`. It is abandoned. This is a new and much
smaller plugin whose only job is the tuner host.
