"""The streamlink process behind a live TS stream: start, deliver, and always clean up.

These drive a real subprocess (tests/fake_streamlink.py) rather than mocking one,
because the failure modes that matter - an orphaned process, a stalled pipe, a
cancelled read - only exist when there is a real process on the other end.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from app.config import get_config
from app.services import live_stream, resolver

FAKE = str(Path(__file__).with_name("fake_streamlink.py"))
PACKET_BYTES = 188


def fake(mode: str, packets: int | None = None) -> list[str]:
    cmd = [sys.executable, FAKE, mode]
    if packets is not None:
        cmd.append(str(packets))
    return cmd


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    # Restarts are off unless a test turns them on, so a fake that exits after
    # its packets behaves like a single run. Backoff is near zero so restart
    # tests do not sleep.
    monkeypatch.setenv("JELLYTTV_LIVE_MAX_RESTARTS_PER_WINDOW", "0")
    monkeypatch.setenv("JELLYTTV_LIVE_RESTART_BACKOFF_SECONDS", "0.01")
    get_config.cache_clear()
    live_stream._active.clear()
    live_stream._starting = 0
    yield
    for handle in list(live_stream._active.values()):
        handle.close()
    live_stream._active.clear()
    live_stream._starting = 0


async def drain(handle: live_stream.LiveStreamHandle) -> bytes:
    body = bytearray()
    async for chunk in handle.iter_bytes():
        body.extend(chunk)
    return bytes(body)


async def test_a_stream_is_delivered_whole_and_in_order_including_the_first_chunk():
    handle = await live_stream.open_stream("alpha", fake("stream", 500))

    body = await drain(handle)

    assert len(body) == 500 * PACKET_BYTES
    assert all(body[i] == 0x47 for i in range(0, len(body), PACKET_BYTES)), "TS sync lost"
    assert handle.bytes_sent == len(body)


async def test_the_process_is_reaped_and_released_when_the_broadcast_ends():
    handle = await live_stream.open_stream("alpha", fake("stream", 100))
    assert live_stream.active_count() == 1

    await drain(handle)
    await handle.reap_task

    assert handle.process.returncode is not None
    assert live_stream.active_count() == 0


async def test_an_offline_channel_is_reported_as_offline_not_as_a_failure():
    """streamlink's own wording is what tells offline apart from broken."""
    with pytest.raises(resolver.ChannelOffline):
        await live_stream.open_stream("alpha", fake("offline"))
    assert live_stream.active_count() == 0


async def test_a_broken_start_carries_streamlinks_reason():
    with pytest.raises(live_stream.StreamStartError, match="403"):
        await live_stream.open_stream("alpha", fake("fail"))
    assert live_stream.active_count() == 0


async def test_a_stream_that_never_produces_bytes_times_out_and_is_killed(monkeypatch):
    monkeypatch.setenv("JELLYTTV_LIVE_STARTUP_TIMEOUT_SECONDS", "1")
    get_config.cache_clear()

    with pytest.raises(live_stream.StreamStartError, match="no stream bytes"):
        await live_stream.open_stream("alpha", fake("silent"))
    assert live_stream.active_count() == 0


async def test_closing_a_live_stream_terminates_the_process():
    """What happens when Jellyfin stops watching: no streamlink left behind."""
    handle = await live_stream.open_stream("alpha", fake("forever"))
    assert handle.process.returncode is None

    await handle.aclose()

    assert handle.process.returncode is not None
    assert live_stream.active_count() == 0


async def test_a_client_disconnect_mid_stream_terminates_the_process():
    """A disconnect cancels the response task while it is awaiting a read.

    Cleanup has to survive that cancellation, which is why `close()` is
    synchronous and the wait-and-kill runs on a detached task.
    """
    handle = await live_stream.open_stream("alpha", fake("forever"))

    async def consume() -> None:
        async for _chunk in handle.iter_bytes():
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert handle.closed
    await handle.reap_task
    assert handle.process.returncode is not None
    assert live_stream.active_count() == 0


async def test_close_is_idempotent():
    handle = await live_stream.open_stream("alpha", fake("forever"))
    await handle.aclose()
    handle.close()
    await handle.aclose()
    assert handle.process.returncode is not None


async def test_the_slot_cap_refuses_a_stream_rather_than_overloading(monkeypatch):
    monkeypatch.setenv("JELLYTTV_MAX_LIVE_STREAMS", "1")
    get_config.cache_clear()

    first = await live_stream.open_stream("alpha", fake("forever"))
    with pytest.raises(live_stream.StreamCapacityError):
        await live_stream.open_stream("beta", fake("forever"))

    await first.aclose()
    # The slot is free again once the first stream is gone.
    second = await live_stream.open_stream("beta", fake("forever"))
    await second.aclose()


async def test_concurrent_opens_cannot_both_take_the_last_slot(monkeypatch):
    monkeypatch.setenv("JELLYTTV_MAX_LIVE_STREAMS", "1")
    get_config.cache_clear()

    results = await asyncio.gather(
        live_stream.open_stream("alpha", fake("forever")),
        live_stream.open_stream("beta", fake("forever")),
        return_exceptions=True,
    )

    handles = [r for r in results if isinstance(r, live_stream.LiveStreamHandle)]
    refused = [r for r in results if isinstance(r, live_stream.StreamCapacityError)]
    assert len(handles) == 1 and len(refused) == 1
    await handles[0].aclose()


async def test_a_flood_of_stderr_does_not_stall_the_stream():
    """streamlink logs to stderr; if nobody read it, a full pipe would block it."""
    handle = await live_stream.open_stream("alpha", fake("chatty", 300))

    body = await asyncio.wait_for(drain(handle), timeout=20)

    assert len(body) == 300 * PACKET_BYTES
    assert handle.stderr_tail, "stderr was not captured"


async def test_the_snapshot_describes_active_streams():
    handle = await live_stream.open_stream("alpha", fake("forever"))
    [entry] = live_stream.snapshot()
    assert entry["login"] == "alpha"
    assert entry["pid"] == handle.process.pid
    await handle.aclose()
    assert live_stream.snapshot() == []


# ------------------------------------------------------------------ restarts
def allow_restarts(monkeypatch, per_window: int, attempts: int = 5, backoff: float = 0.01) -> None:
    monkeypatch.setenv("JELLYTTV_LIVE_MAX_RESTARTS_PER_WINDOW", str(per_window))
    monkeypatch.setenv("JELLYTTV_LIVE_RESTART_ATTEMPTS", str(attempts))
    monkeypatch.setenv("JELLYTTV_LIVE_RESTART_BACKOFF_SECONDS", str(backoff))
    get_config.cache_clear()


async def test_a_dropped_streamlink_is_restarted_into_the_same_response(monkeypatch):
    """streamlink never re-resolves in place; when its playlist url dies it
    exits. Jellyfin must keep reading one unbroken stream rather than hit EOF
    and have to reopen the tuner."""
    allow_restarts(monkeypatch, per_window=2)
    handle = await live_stream.open_stream("alpha", fake("stream", 100))

    body = await drain(handle)

    assert handle.restarts == 2
    assert len(body) == 3 * 100 * PACKET_BYTES, "every run belongs to the one response"
    assert all(body[i] == 0x47 for i in range(0, len(body), PACKET_BYTES)), "TS sync lost"


async def test_a_broadcast_that_ends_closes_the_stream_instead_of_restarting(monkeypatch, tmp_path):
    allow_restarts(monkeypatch, per_window=5)
    state = str(tmp_path / "ran")
    handle = await live_stream.open_stream(
        "alpha", [sys.executable, FAKE, "once-then-offline", "100", state]
    )

    body = await drain(handle)
    await handle.reap_task

    assert len(body) == 100 * PACKET_BYTES
    assert handle.restarts == 0
    assert live_stream.active_count() == 0


async def test_a_restart_that_keeps_failing_gives_up_and_ends(monkeypatch, tmp_path):
    allow_restarts(monkeypatch, per_window=5, attempts=2)
    state = str(tmp_path / "ran")
    handle = await live_stream.open_stream(
        "alpha", [sys.executable, FAKE, "once-then-fail", "100", state]
    )

    body = await asyncio.wait_for(drain(handle), timeout=20)
    await handle.reap_task

    assert len(body) == 100 * PACKET_BYTES
    assert handle.restarts == 0
    assert live_stream.active_count() == 0


async def test_a_restart_loop_is_capped_so_a_broken_stream_cannot_spin(monkeypatch):
    allow_restarts(monkeypatch, per_window=3)
    handle = await live_stream.open_stream("alpha", fake("stream", 20))

    await asyncio.wait_for(drain(handle), timeout=20)

    assert handle.restarts == 3


async def test_a_disconnect_during_a_restart_leaves_no_process_behind(monkeypatch):
    allow_restarts(monkeypatch, per_window=5, backoff=2.0)
    handle = await live_stream.open_stream("alpha", fake("stream", 20))

    async def consume() -> None:
        async for _chunk in handle.iter_bytes():
            pass

    task = asyncio.create_task(consume())
    # The first run is long finished; the stream is now in its restart backoff.
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert handle.closed
    assert handle.restarts == 0, "cancelled before any replacement was started"
    await handle.reap_task
    assert handle.process.returncode is not None
    assert live_stream.active_count() == 0
