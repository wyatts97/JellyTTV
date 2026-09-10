# The optional Jellyfin plugin

**Status: design, not built.** This document exists so the work is understood
before it starts, and so the limitation it removes is written down rather than
rediscovered.

## The problem it solves

JellyTTV integrates with Jellyfin through the built-in **M3U tuner host**:
`backend/app/routers/tuner.py` serves `playlist.m3u` and `guide.xml`, and
Jellyfin is pointed at them as an ordinary IPTV source.

That path has one consequence that shapes everything downstream. Playback goes
through **server-side ffmpeg**, whose HLS demuxer
[does not implement `#EXT-X-DISCONTINUITY`](https://trac.ffmpeg.org/ticket/5419)
and cannot follow a mid-stream format change.

On Jellyfin **12.0** that is true for every client, and it is enforced twice over
in the server itself:

* `M3UTunerHost.CreateMediaSourceInfo` sets `SupportsDirectPlay = false` when the
  channel URL's path ends in `.m3u8`, `.m3u` or `.mpd`.
* `StreamBuilder` independently rules out direct play for any source that probes
  as `hls`, `applehttp` or `dash`.

Both landed in [PR #17768](https://github.com/jellyfin/jellyfin/pull/17768). On
10.11 only jellyfin-web was affected, because a live HLS source was not in its
`DirectPlayProfiles`; native clients handed the URL to their own player and were
fine. That is no longer the case.

`SupportsDirectStream` stays true, so 12.0 **remuxes** rather than fully
transcodes — cheaper than what the web client used to do, but strictly worse than
what native clients had, and it puts that same demuxer in front of everybody.

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
correctly. So do AVPlayer on iOS, ExoPlayer on Android, and the player in most TV
clients. **On 10.11 that made native clients fine and this document a web-client
problem. On 12.0 no client gets to use its own player**, so the mitigation in
`stream_session.py` — never cutting a hole in the timeline, holding on
`assets/hold.ts` while a clean backup is found — is now what protects every
client rather than just the browser.

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

### The part that needs verifying first — and 12.0 makes it harder

**On Jellyfin 12.0 the `MediaSourceInfo` above is not enough, and may not be
achievable at all.** `SupportsDirectPlay = true` is set by the tuner host, but
`StreamBuilder` overrules it afterwards for any source whose container is `hls`,
`applehttp` or `dash` — and the sketch above declares `Container = "hls"`. The
`M3UTunerHost` extension check is private to that class so a plugin escapes it,
but the `StreamBuilder` check applies to every source regardless of origin.

So before writing anything, establish whether a plugin can present the stream in
a way that never probes as a manifest — which realistically means not handing the
client an HLS playlist at all. If it cannot, this design does not work on 12.0
and the honest options are the two in "If direct play cannot be restored" below.

Also note that 12.0 targets **.NET 10**, so any plugin has to be built against
it; plugins compiled for 10.11 will not load.

Whatever is attempted, verify it empirically: build a minimal tuner host, point a
client at it, and confirm from Dashboard → Logs that no ffmpeg process starts.

If the profile blocks it, the fallback is the older approach: inject a client
script that forces hls.js for JellyTTV URLs. Note that this repository already
tried DOM and script injection in the now-abandoned `jellyfin-plugin-jellyttv`,
and it broke repeatedly across Jellyfin versions. Treat it as the fallback, not
the plan.

### If direct play cannot be restored

Two honest fallbacks, neither of them this plugin:

1. **Accept the remux.** This is what JellyTTV does today. The timeline is never
   cut, so there is no jump for the demuxer to mishandle; what remains is the
   freeze at a format change, which is a known and bounded cost.
2. **Stop serving a manifest.** A single continuous MPEG-TS byte stream at an
   extensionless URL, served as `video/MP2T`, is not a manifest to either check —
   and `M3UTunerHost` would additionally share one upstream connection across
   viewers via `SharedHttpStream`, which a `.m3u8` URL can never do. This is a
   large change and mid-stream format changes stay hard, but it is the only route
   that removes the demuxer rather than working around it.

### The second win is already banked

This document used to claim a plugin as the way to drop the guide-cache hack.
Jellyfin 12.0 removed the need: `SaveListingProvider` now deletes the cached
`<cache>/xmltv/<provider-id>.xml` and queues the refresh itself, so saving the
provider unchanged is the whole operation. See `JellyfinClient.refresh_guide_now`.

## Explicitly out of scope

Reviving `jellyfin-plugin-jellyttv`. It is abandoned. This is a new and much
smaller plugin whose only job is the tuner host.
