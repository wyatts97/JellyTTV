"""Re-encode a channel into one continuous, format-stable HLS output.

Everything upstream of this module works at the level of *which segments should
play*. This module answers a different question: how do they reach Jellyfin
without carrying the upstream's timing and format changes along with them.

Why it exists
-------------
ffmpeg's HLS demuxer does not implement `#EXT-X-DISCONTINUITY` (ffmpeg trac
#5419, open for years - it does not even set `AVFMT_TS_DISCONT`). Jellyfin's web
client cannot avoid that demuxer: a live HLS source is not in jellyfin-web's
DirectPlayProfiles, so the direct-play check fails and playback falls back to
server-side ffmpeg even though hls.js would have handled the discontinuity fine.

The consequence is that anything discontinuous we hand Jellyfin arrives as an
uncompensated timestamp jump - and video and audio absorb that jump differently,
so they drift apart by the size of the gap and stay drifted. `stream_session`
avoids *creating* such gaps. This module removes the remaining class of them:
the ones upstream hands us, and the format changes that come with splicing a
different rendition over an ad break.

How
---
A long-lived ffmpeg per session reads concatenated MPEG-TS on **stdin** and
writes HLS. Feeding it a playlist instead would put the discontinuity back in
front of the same demuxer we are trying to get away from; feeding it bytes means
a source switch arrives as an ordinary stream change with fresh PAT/PMT.

Two flags carry the whole guarantee:

* ``-use_wallclock_as_timestamps 1`` throws the input's timestamps away and
  stamps by arrival, so no upstream jump can propagate;
* a constant frame rate and a fixed scale/pad make every source produce the same
  output format, so a 360p backup spliced over a 1080p stream is invisible.

A feeder that stalls therefore produces duplicated frames - a freeze - and never
a desync. That is the trade this module exists to make.

The cost is real: a full transcode per active channel, on top of the one
Jellyfin is already running. It is off by default for that reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.logging_conf import get_logger

log = get_logger(__name__)

FFMPEG_BIN = "ffmpeg"

# Output shape. Fixed for the life of a session: the entire point is that the
# format Jellyfin probes at the start is the format it gets for ever after.
OUTPUT_SEGMENT_SECONDS = 2
OUTPUT_LIST_SIZE = 10
OUTPUT_GOP_SECONDS = 2

# How long to wait for the encoder to produce a playable playlist before giving
# up on it for this request. Deliberately below the router's own deadline.
STARTUP_TIMEOUT = 5.0
# A playlist needs a couple of segments before a player can do anything useful.
MIN_STARTUP_SEGMENTS = 2

# Restarts closer together than this mean the encoder is failing for a reason a
# restart will not fix (bad flags, missing codec), so stop hammering it.
MIN_RESTART_INTERVAL = 10.0
MAX_CONSECUTIVE_RESTARTS = 3

# How many fed segment URIs to remember. Comfortably more than any sliding
# window, so a segment cannot be re-offered after being forgotten, and small
# enough that a long session does not grow without bound.
ACCEPTED_MEMORY = 512

HWACCELS = ("none", "vaapi", "nvenc", "qsv")

_VIDEO_ENCODERS = {
    "none": "libx264",
    "vaapi": "h264_vaapi",
    "nvenc": "h264_nvenc",
    "qsv": "h264_qsv",
}


def resolve_hwaccel(value: str | None) -> str:
    """Normalise the configured accelerator (NULL/blank/unknown -> software)."""
    accel = (value or "").strip().lower()
    return accel if accel in HWACCELS else "none"


def available() -> bool:
    return shutil.which(FFMPEG_BIN) is not None


# Fetches one segment's bytes. Injected so the module stays free of httpx and
# can be tested with plain byte strings.
SegmentFetcher = Callable[[str], Awaitable[bytes]]

# Spawns the encoder. Injected for the same reason - the tests assert on the
# argv this module builds without ever starting a real process.
ProcessLauncher = Callable[[list[str]], Awaitable[asyncio.subprocess.Process]]


async def _spawn(argv: list[str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )


def build_command(
    *,
    workdir: Path,
    width: int,
    height: int,
    fps: int,
    hwaccel: str = "none",
) -> list[str]:
    """The encoder invocation. Pure, so a test can read it without running it."""
    encoder = _VIDEO_ENCODERS[resolve_hwaccel(hwaccel)]
    gop = str(fps * OUTPUT_GOP_SECONDS)

    argv = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        # Discard the input's own timing entirely. This is the flag that makes an
        # upstream discontinuity a non-event: whatever the source thought the
        # time was, output timestamps come from when the bytes arrived.
        "-fflags",
        "+genpts+igndts",
        "-use_wallclock_as_timestamps",
        "1",
        "-f",
        "mpegts",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
    ]

    # Scale-and-pad rather than plain scale: a backup at a different aspect
    # ratio must still fill exactly the same frame, or the output geometry
    # changes and we are back to the problem this module solves.
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={fps}"
    )

    argv += ["-c:v", encoder, "-vf", vf]
    if encoder == "libx264":
        argv += ["-preset", "veryfast", "-tune", "zerolatency"]
    argv += [
        # A closed GOP on a fixed cadence keeps every output segment
        # independently decodable, so a player joining mid-stream never waits.
        "-g",
        gop,
        "-keyint_min",
        gop,
        "-sc_threshold",
        "0",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-f",
        "hls",
        "-hls_time",
        str(OUTPUT_SEGMENT_SECONDS),
        "-hls_list_size",
        str(OUTPUT_LIST_SIZE),
        # `omit_endlist` keeps it a live playlist; `delete_segments` is what
        # bounds the work directory without anyone having to sweep it.
        "-hls_flags",
        "delete_segments+omit_endlist+independent_segments",
        "-hls_segment_type",
        "mpegts",
        "-hls_segment_filename",
        str(workdir / "seg%05d.ts"),
        str(workdir / "index.m3u8"),
    ]
    return argv


@dataclass
class NormaliserStats:
    started: int = 0
    restarts: int = 0
    segments_fed: int = 0
    bytes_fed: int = 0
    fetch_failures: int = 0
    feed_failures: int = 0


@dataclass
class Normaliser:
    """One encoder, and the queue of segments being fed into it."""

    key: str
    login: str
    workdir: Path
    width: int
    height: int
    fps: int
    hwaccel: str = "none"

    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    feeder: asyncio.Task | None = field(default=None, repr=False)
    # Bounded on purpose: if the encoder cannot keep up, dropping the oldest
    # queued segment is right. Buffering minutes of video would only add latency
    # to a stream that is already behind the live edge.
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(maxsize=8))
    # Every segment ever accepted, not just the ones still waiting. The caller
    # re-offers its whole sliding window on every poll, so dropping an entry
    # once it had been fed would mux the same seconds of video in again and
    # again - the stutter this module exists to prevent rather than cause.
    # Bounded and FIFO-evicted, because a session runs for hours.
    accepted: OrderedDict[str, None] = field(default_factory=OrderedDict)

    started_at: float = 0.0
    last_start_at: float = 0.0
    consecutive_restarts: int = 0
    failed: bool = False

    stats: NormaliserStats = field(default_factory=NormaliserStats)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def playlist_path(self) -> Path:
        return self.workdir / "index.m3u8"

    def segment_count(self) -> int:
        try:
            return len(list(self.workdir.glob("seg*.ts")))
        except OSError:
            return 0

    def ready(self) -> bool:
        return (
            not self.failed
            and self.playlist_path.exists()
            and self.segment_count() >= MIN_STARTUP_SEGMENTS
        )

    def snapshot(self) -> dict:
        return {
            "login": self.login,
            "workdir": str(self.workdir),
            "output": f"{self.width}x{self.height}@{self.fps}",
            "hwaccel": self.hwaccel,
            "running": self.process is not None and self.process.returncode is None,
            "ready": self.ready(),
            "failed": self.failed,
            "queue_depth": self.queue.qsize(),
            "age_s": round(time.monotonic() - self.started_at, 1) if self.started_at else 0.0,
            "stats": vars(self.stats),
        }


_normalisers: dict[str, Normaliser] = {}
_registry_lock = asyncio.Lock()


async def _start_process(
    norm: Normaliser, launch: ProcessLauncher, fetch_segment: SegmentFetcher
) -> None:
    """Spawn the encoder and the task that feeds it. Caller holds `norm.lock`."""
    now = time.monotonic()
    if now - norm.last_start_at < MIN_RESTART_INTERVAL and norm.stats.started:
        norm.consecutive_restarts += 1
        if norm.consecutive_restarts >= MAX_CONSECUTIVE_RESTARTS:
            # Restarting is only worth doing for a transient failure. Three in
            # quick succession means the invocation itself is wrong - a codec
            # the host does not have, a device that is not there - and retrying
            # forever would just burn CPU while the channel stays broken. Give
            # up so the caller falls back to serving upstream directly.
            norm.failed = True
            log.error(
                "normaliser keeps dying; falling back to the passthrough proxy",
                login=norm.login,
                restarts=norm.consecutive_restarts,
            )
            return
    else:
        norm.consecutive_restarts = 0

    norm.workdir.mkdir(parents=True, exist_ok=True)
    argv = build_command(
        workdir=norm.workdir,
        width=norm.width,
        height=norm.height,
        fps=norm.fps,
        hwaccel=norm.hwaccel,
    )
    norm.process = await launch(argv)
    norm.last_start_at = now
    if not norm.started_at:
        norm.started_at = now
    norm.stats.started += 1
    norm.feeder = asyncio.create_task(_feed(norm, fetch_segment, launch))
    log.info(
        "normaliser started",
        login=norm.login,
        output=f"{norm.width}x{norm.height}@{norm.fps}",
        hwaccel=norm.hwaccel,
    )


async def _feed(
    norm: Normaliser, fetch_segment: SegmentFetcher, launch: ProcessLauncher
) -> None:
    """Pump queued segments into the encoder's stdin, in order, forever.

    Strictly sequential: MPEG-TS on a pipe is a byte stream, so two concurrent
    writers would interleave packets and produce garbage. Ordering is the whole
    contract here.
    """
    while True:
        uri = await norm.queue.get()
        process = norm.process
        if process is None or process.stdin is None:
            return
        try:
            payload = await fetch_segment(uri)
        except Exception as exc:  # noqa: BLE001 - a bad segment must not kill the feed
            norm.stats.fetch_failures += 1
            log.debug("normaliser segment fetch failed", login=norm.login, error=str(exc)[:160])
            continue
        if not payload:
            norm.stats.fetch_failures += 1
            continue
        try:
            process.stdin.write(payload)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            # The encoder died underneath us. Restarting is handled by the next
            # `ensure` call, which sees a dead process; this task just stops.
            norm.stats.feed_failures += 1
            log.warning("normaliser input closed", login=norm.login, error=str(exc)[:160])
            return
        norm.stats.segments_fed += 1
        norm.stats.bytes_fed += len(payload)


async def ensure(
    *,
    key: str,
    login: str,
    workdir: Path,
    width: int,
    height: int,
    fps: int,
    hwaccel: str = "none",
    fetch_segment: SegmentFetcher,
    launch: ProcessLauncher = _spawn,
) -> Normaliser | None:
    """Get the encoder for this session, starting or restarting it as needed.

    Returns None once the encoder has proven unstartable, which is the caller's
    signal to serve the upstream playlist directly instead. A channel that plays
    with ads beats a channel that does not play.
    """
    async with _registry_lock:
        norm = _normalisers.get(key)
        if norm is None:
            norm = Normaliser(
                key=key,
                login=login,
                workdir=workdir,
                width=width,
                height=height,
                fps=fps,
                hwaccel=resolve_hwaccel(hwaccel),
            )
            _normalisers[key] = norm

    async with norm.lock:
        if norm.failed:
            return None
        if norm.process is None or norm.process.returncode is not None:
            if norm.process is not None:
                norm.stats.restarts += 1
                log.warning(
                    "normaliser exited; restarting",
                    login=login,
                    returncode=norm.process.returncode,
                )
            await _start_process(norm, launch, fetch_segment)
            if norm.failed:
                return None
    return norm


def submit(norm: Normaliser, uris: list[str]) -> int:
    """Queue segments for encoding, skipping any already queued.

    Non-blocking by construction: a full queue drops the segment rather than
    holding up the playlist request that offered it. The encoder falling behind
    is a capacity problem, and making the client wait would not fix it.
    """
    count = 0
    for uri in uris:
        if uri in norm.accepted:
            continue
        try:
            norm.queue.put_nowait(uri)
        except asyncio.QueueFull:
            log.debug("normaliser queue full; dropping segment", login=norm.login)
            break
        norm.accepted[uri] = None
        count += 1
    while len(norm.accepted) > ACCEPTED_MEMORY:
        norm.accepted.popitem(last=False)
    return count


async def wait_ready(norm: Normaliser, timeout: float = STARTUP_TIMEOUT) -> bool:
    """Wait for the encoder to produce enough output to be worth serving."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if norm.ready():
            return True
        if norm.process is not None and norm.process.returncode is not None:
            return False
        await asyncio.sleep(0.1)
    return norm.ready()


async def stop(key: str) -> None:
    """Tear an encoder down and remove its work directory."""
    async with _registry_lock:
        norm = _normalisers.pop(key, None)
    if norm is None:
        return

    if norm.feeder is not None and not norm.feeder.done():
        norm.feeder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await norm.feeder
    process = norm.process
    if process is not None and process.returncode is None:
        with contextlib.suppress(Exception):
            if process.stdin is not None:
                process.stdin.close()
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
    shutil.rmtree(norm.workdir, ignore_errors=True)
    log.info("normaliser stopped", login=norm.login, stats=vars(norm.stats))


def get(key: str) -> Normaliser | None:
    return _normalisers.get(key)


def active_keys() -> list[str]:
    return list(_normalisers)


def stats() -> list[dict]:
    return [n.snapshot() for n in _normalisers.values()]
