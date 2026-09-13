"""Live channels as one continuous MPEG-TS stream, straight from streamlink.

This replaces serving Jellyfin a playlist we rewrote ourselves. That design made
JellyTTV a stateful live HLS origin in front of Jellyfin's ffmpeg, and every way
it could be imperfect froze playback: a recreated session restarting the media
sequence at 0 (ffmpeg waits for the old sequence to come back, then gives up), a
dead upstream url served from cache for an hour, resolves cancelled by a render
deadline. Jellyfin 12 also refuses to direct-play a manifest, so it re-muxed our
HLS into a second HLS of its own.

Here streamlink runs its own HLS client - reloads, segment retries, weaver
reassignment - and writes the stream to stdout. We hand those bytes to Jellyfin
as they arrive. There is no playlist window, no sequence number and no manifest
left anywhere in the path.

streamlink does not refresh its access token or re-resolve mid-stream: when the
playlist url it was given stops working, it exits. So an exit that the client did
not cause is not treated as the end of the stream. streamlink is started again
and its output is appended to the same response, so Jellyfin keeps reading one
unbroken byte stream instead of hitting EOF and having to reopen the tuner. Only
a broadcast that has genuinely ended - or a restart loop that keeps failing -
ends the response.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from collections import deque
from dataclasses import dataclass, field

from app.config import get_config
from app.logging_conf import get_logger
from app.services import resolver

log = get_logger(__name__)

CHUNK_BYTES = 64 * 1024
STDERR_TAIL_LINES = 50
# How long a terminated streamlink gets to exit before it is killed.
TERMINATE_GRACE_SECONDS = 5.0
# Restarts are counted over this window; too many inside it means something is
# persistently wrong and restarting again will not fix it.
RESTART_WINDOW_SECONDS = 300.0
MAX_RESTART_BACKOFF_SECONDS = 8.0


class StreamStartError(RuntimeError):
    """streamlink did not produce any stream bytes."""


class StreamCapacityError(RuntimeError):
    """Every live stream slot is already in use."""


@dataclass
class _Spawned:
    process: asyncio.subprocess.Process
    stderr_task: asyncio.Task
    stderr_tail: deque[str]
    first_chunk: bytes


@dataclass(eq=False)
class LiveStreamHandle:
    id: int
    login: str
    cmd: list[str]
    process: asyncio.subprocess.Process
    first_chunk: bytes
    stderr_tail: deque[str]
    stderr_task: asyncio.Task
    started_at: float = field(default_factory=time.monotonic)
    bytes_sent: int = 0
    closed: bool = False
    reap_task: asyncio.Task | None = None
    restarts: int = 0
    restart_times: deque[float] = field(default_factory=deque)

    async def iter_bytes(self):
        """Yield the stream until it genuinely ends or the client goes away.

        Cleanup lives in `finally`, so it runs on a normal end, on an error, and
        when the response is cancelled because the client disconnected.
        """
        try:
            pending = self.first_chunk
            while pending:
                self.bytes_sent += len(pending)
                yield pending
                assert self.process.stdout is not None
                pending = await self.process.stdout.read(CHUNK_BYTES)
                if not pending and not self.closed:
                    pending = await self._restart()
        finally:
            self.close()

    async def _restart(self) -> bytes:
        """Replace an exited streamlink. Returns its first chunk, or b"" to end.

        Deliberately conservative about when to give up: an offline channel ends
        the stream at once, and so does a restart loop that keeps coming back.
        Anything else gets a few attempts with backoff, because the common cause
        - streamlink's playlist url expiring on a long watch - is exactly the
        case a fresh resolve fixes.
        """
        cfg = get_config()
        await _retire(self.login, self.process, self.stderr_task, self.stderr_tail)

        now = time.monotonic()
        while self.restart_times and now - self.restart_times[0] > RESTART_WINDOW_SECONDS:
            self.restart_times.popleft()
        if len(self.restart_times) >= cfg.live_max_restarts_per_window:
            log.warning(
                "live stream keeps dropping; ending it rather than restarting again",
                login=self.login,
                restarts_in_window=len(self.restart_times),
            )
            return b""

        for attempt in range(cfg.live_restart_attempts):
            delay = min(cfg.live_restart_backoff_seconds * (2**attempt), MAX_RESTART_BACKOFF_SECONDS)
            await asyncio.sleep(delay)
            if self.closed:
                return b""
            try:
                spawned = await _spawn(self.login, self.cmd, cfg.live_startup_timeout_seconds)
            except resolver.ChannelOffline:
                log.info("broadcast ended; closing live stream", login=self.login)
                return b""
            except StreamStartError as exc:
                log.warning(
                    "live stream restart attempt failed",
                    login=self.login,
                    attempt=attempt + 1,
                    error=str(exc)[:300],
                )
                continue
            if self.closed:
                # The client went away while the replacement was starting.
                await _abandon(spawned.process, spawned.stderr_task)
                return b""
            self.process = spawned.process
            self.stderr_task = spawned.stderr_task
            self.stderr_tail = spawned.stderr_tail
            self.restarts += 1
            self.restart_times.append(time.monotonic())
            log.info(
                "live stream restarted in place",
                login=self.login,
                restarts=self.restarts,
                attempt=attempt + 1,
            )
            return spawned.first_chunk

        log.warning(
            "live stream could not be restarted; ending it",
            login=self.login,
            attempts=cfg.live_restart_attempts,
        )
        return b""

    def close(self) -> None:
        """Stop the process. Synchronous, so a cancelled caller cannot skip it.

        Terminating is immediate; waiting for the exit (and escalating to a
        kill) happens on a detached task that nothing can cancel out from under
        us, so no streamlink is ever left orphaned.
        """
        if self.closed:
            return
        self.closed = True
        _active.pop(self.id, None)
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
        self.reap_task = asyncio.create_task(self._reap())
        _reap_tasks.add(self.reap_task)
        self.reap_task.add_done_callback(_reap_tasks.discard)

    async def aclose(self) -> None:
        """Close and wait until the process has been reaped."""
        self.close()
        if self.reap_task is not None:
            await self.reap_task

    async def _reap(self) -> None:
        await _stop(self.process)
        self.stderr_task.cancel()
        log.info(
            "live stream closed",
            login=self.login,
            exit_code=self.process.returncode,
            restarts=self.restarts,
            seconds=round(time.monotonic() - self.started_at, 1),
            megabytes=round(self.bytes_sent / 1_000_000, 1),
            stderr_tail=list(self.stderr_tail)[-5:],
        )


_ids = itertools.count(1)
_active: dict[int, LiveStreamHandle] = {}
_starting = 0
_reap_tasks: set[asyncio.Task] = set()


async def _drain_stderr(process: asyncio.subprocess.Process, tail: deque[str]) -> None:
    """Keep stderr flowing; a full pipe would stall streamlink itself."""
    assert process.stderr is not None
    while True:
        line = await process.stderr.readline()
        if not line:
            return
        tail.append(line.decode("utf-8", "replace").rstrip())


async def _stop(process: asyncio.subprocess.Process) -> None:
    """Wait for a process to exit, escalating to a kill after the grace period."""
    try:
        await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


async def _retire(
    login: str,
    process: asyncio.subprocess.Process,
    stderr_task: asyncio.Task,
    tail: deque[str],
) -> None:
    """Reap a streamlink whose output ended, and record why it stopped."""
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
    await _stop(process)
    # Let the drain collect streamlink's last words before logging them.
    try:
        await asyncio.wait_for(stderr_task, timeout=2.0)
    except TimeoutError:
        stderr_task.cancel()
    log.warning(
        "streamlink exited mid-stream",
        login=login,
        exit_code=process.returncode,
        stderr_tail=list(tail)[-5:],
    )


async def _abandon(process: asyncio.subprocess.Process, stderr_task: asyncio.Task) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
    stderr_task.cancel()


async def _spawn(login: str, cmd: list[str], startup_timeout: float) -> _Spawned:
    """Start streamlink and wait for its first stream bytes.

    Raises `resolver.ChannelOffline` or `StreamStartError`, and never leaves a
    process behind when it does.
    """
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
    stderr_task = asyncio.create_task(_drain_stderr(process, tail))
    assert process.stdout is not None

    try:
        first = await asyncio.wait_for(process.stdout.read(CHUNK_BYTES), timeout=startup_timeout)
    except TimeoutError:
        await _abandon(process, stderr_task)
        raise StreamStartError(
            f"streamlink produced no stream bytes within {startup_timeout:.0f}s: "
            + " | ".join(list(tail)[-3:])
        ) from None
    except BaseException:
        # Cancelled mid-start (the client gave up): never leave the process.
        await _abandon(process, stderr_task)
        raise

    if not first:
        await process.wait()
        try:
            await asyncio.wait_for(stderr_task, timeout=2.0)
        except TimeoutError:
            stderr_task.cancel()
        detail = " | ".join(list(tail)[-3:]) or f"exit {process.returncode}"
        if resolver.looks_offline(" ".join(tail)):
            raise resolver.ChannelOffline(f"{login} is offline")
        raise StreamStartError(f"streamlink exited before streaming: {detail}")

    return _Spawned(process=process, stderr_task=stderr_task, stderr_tail=tail, first_chunk=first)


async def open_stream(login: str, cmd: list[str]) -> LiveStreamHandle:
    """Start streamlink and return once real stream bytes have arrived.

    Waiting for the first chunk before answering is what lets the caller send a
    genuine 503 for an offline channel, rather than a 200 that ends at once.

    Raises `resolver.ChannelOffline`, `StreamStartError` or
    `StreamCapacityError`.
    """
    global _starting
    cfg = get_config()
    # No await between the check and the reservation, so concurrent opens
    # cannot both squeeze into the last slot.
    if len(_active) + _starting >= cfg.max_live_streams:
        raise StreamCapacityError(f"all {cfg.max_live_streams} live stream slots are in use")
    _starting += 1
    try:
        log.info("starting live stream", login=login)
        spawned = await _spawn(login, cmd, cfg.live_startup_timeout_seconds)
    finally:
        _starting -= 1

    handle = LiveStreamHandle(
        id=next(_ids),
        login=login,
        cmd=cmd,
        process=spawned.process,
        first_chunk=spawned.first_chunk,
        stderr_tail=spawned.stderr_tail,
        stderr_task=spawned.stderr_task,
    )
    _active[handle.id] = handle
    log.info("live stream started", login=login, active=len(_active))
    return handle


def snapshot() -> list[dict]:
    """Active streams, for the debug endpoint."""
    now = time.monotonic()
    return [
        {
            "login": h.login,
            "pid": h.process.pid,
            "age_s": round(now - h.started_at, 1),
            "megabytes": round(h.bytes_sent / 1_000_000, 2),
            "restarts": h.restarts,
            "stderr_tail": list(h.stderr_tail)[-5:],
        }
        for h in _active.values()
    ]


def active_count() -> int:
    return len(_active)
