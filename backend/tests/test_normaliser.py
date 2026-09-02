"""The output normaliser.

No ffmpeg is spawned here. The process launcher and the segment fetcher are
injected for exactly that reason, so these tests can pin the invocation and the
feeder's behaviour without needing a codec on the machine running them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services import normaliser


class FakeProcess:
    """Stands in for `asyncio.subprocess.Process` closely enough to feed."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdin = FakeStdin()
        self.stderr = FakeStderr([])
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


class FakeStderr:
    """Yields the given lines, then EOF - like a real pipe being drained."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)
        self.fully_read = False

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        self.fully_read = True
        return b""


class FakeStdin:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False
        self.broken = False

    def write(self, data: bytes) -> None:
        if self.broken:
            raise BrokenPipeError("encoder went away")
        self.written.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def make_launcher(processes: list[FakeProcess] | None = None):
    """A launcher that records argv and hands back successive fake processes."""
    seen: list[list[str]] = []
    queue = list(processes or [])

    async def launch(argv: list[str]) -> FakeProcess:
        seen.append(argv)
        return queue.pop(0) if queue else FakeProcess()

    return launch, seen


@pytest.fixture
async def stopped():
    yield
    for key in normaliser.active_keys():
        await normaliser.stop(key)


# ------------------------------------------------------------------- the argv
def test_the_invocation_discards_upstream_timestamps(tmp_path: Path):
    """The flag the whole design rests on.

    `-use_wallclock_as_timestamps 1` means output timing comes from when bytes
    arrived, not from what the source claimed - so an upstream discontinuity,
    which ffmpeg's HLS demuxer would otherwise pass through uncompensated,
    cannot reach Jellyfin at all.
    """
    argv = normaliser.build_command(workdir=tmp_path, width=1920, height=1080, fps=60)
    assert "-use_wallclock_as_timestamps" in argv
    assert argv[argv.index("-use_wallclock_as_timestamps") + 1] == "1"
    # Input is a byte stream, never a playlist: handing ffmpeg our own m3u8 would
    # put the discontinuity back in front of the demuxer we are escaping.
    assert argv[argv.index("-i") + 1] == "pipe:0"
    assert argv[argv.index("-f") + 1] == "mpegts"


def test_every_source_is_forced_into_one_fixed_shape(tmp_path: Path):
    """What makes a mismatched backup safe to splice."""
    argv = normaliser.build_command(workdir=tmp_path, width=1280, height=720, fps=30)
    vf = argv[argv.index("-vf") + 1]
    assert "scale=1280:720:force_original_aspect_ratio=decrease" in vf
    # Pad, not just scale: a different aspect ratio must still fill the same
    # frame or the output geometry changes and the problem comes back.
    assert "pad=1280:720" in vf
    assert "fps=30" in vf
    # A closed GOP on a fixed cadence keeps every output segment independently
    # decodable, so a player joining mid-stream never waits for a keyframe.
    assert argv[argv.index("-g") + 1] == "60"
    assert argv[argv.index("-keyint_min") + 1] == "60"


def test_the_output_stays_a_live_playlist(tmp_path: Path):
    argv = normaliser.build_command(workdir=tmp_path, width=1920, height=1080, fps=60)
    flags = argv[argv.index("-hls_flags") + 1]
    assert "omit_endlist" in flags, "an ENDLIST would tell the player the stream ended"
    assert "delete_segments" in flags, "the work directory has to stay bounded"


@pytest.mark.parametrize(
    ("hwaccel", "encoder"),
    [("none", "libx264"), ("vaapi", "h264_vaapi"), ("nvenc", "h264_nvenc"), ("qsv", "h264_qsv")],
)
def test_the_encoder_follows_the_configured_accelerator(tmp_path: Path, hwaccel, encoder):
    argv = normaliser.build_command(
        workdir=tmp_path, width=1920, height=1080, fps=60, hwaccel=hwaccel
    )
    assert argv[argv.index("-c:v") + 1] == encoder


def test_an_unknown_accelerator_falls_back_to_software(tmp_path: Path):
    """An upgraded database can hold anything; it must not break playback."""
    assert normaliser.resolve_hwaccel("videotoolbox") == "none"
    assert normaliser.resolve_hwaccel(None) == "none"
    argv = normaliser.build_command(
        workdir=tmp_path, width=1920, height=1080, fps=60, hwaccel="nonsense"
    )
    assert argv[argv.index("-c:v") + 1] == "libx264"


# ------------------------------------------------------------------ the feeder
async def test_segments_reach_the_encoder_in_order(tmp_path: Path, stopped):
    """MPEG-TS on a pipe is a byte stream, so ordering is the whole contract."""
    process = FakeProcess()
    launch, _ = make_launcher([process])

    async def fetch(uri: str) -> bytes:
        return uri.encode()

    norm = await normaliser.ensure(
        key="adapt:best",
        login="adapt",
        workdir=tmp_path / "adapt",
        width=1920,
        height=1080,
        fps=60,
        fetch_segment=fetch,
        launch=launch,
    )
    assert norm is not None
    normaliser.submit(norm, ["a.ts", "b.ts", "c.ts"])
    for _ in range(40):
        if len(process.stdin.written) == 3:
            break
        await asyncio.sleep(0.01)

    assert process.stdin.written == [b"a.ts", b"b.ts", b"c.ts"]


async def test_a_segment_is_never_fed_twice(tmp_path: Path, stopped):
    """The caller re-offers its whole sliding window on every poll.

    Only what is new may be encoded. Deduping against the *queue* alone is not
    enough - a segment leaves the queue the moment it is fed, so the next poll
    would offer it again and the same seconds of video would be muxed in twice.
    """
    process = FakeProcess()
    launch, _ = make_launcher([process])

    async def fetch(uri: str) -> bytes:
        return uri.encode()

    norm = await normaliser.ensure(
        key="adapt:best",
        login="adapt",
        workdir=tmp_path / "adapt",
        width=1920,
        height=1080,
        fps=60,
        fetch_segment=fetch,
        launch=launch,
    )
    assert norm is not None

    # A first poll's window, fed all the way through before the next poll.
    assert normaliser.submit(norm, ["a.ts", "b.ts"]) == 2
    for _ in range(40):
        if len(process.stdin.written) == 2:
            break
        await asyncio.sleep(0.01)
    assert process.stdin.written == [b"a.ts", b"b.ts"]

    # The next poll still carries the old segments plus one new one.
    assert normaliser.submit(norm, ["a.ts", "b.ts", "c.ts"]) == 1, "only the new one"
    for _ in range(40):
        if len(process.stdin.written) == 3:
            break
        await asyncio.sleep(0.01)
    assert process.stdin.written == [b"a.ts", b"b.ts", b"c.ts"]


async def test_a_failing_segment_does_not_kill_the_feed(tmp_path: Path, stopped):
    """One bad segment is a hiccup; a dead feeder is a dead channel."""
    process = FakeProcess()
    launch, _ = make_launcher([process])

    async def fetch(uri: str) -> bytes:
        if uri == "bad.ts":
            raise RuntimeError("404 from the CDN")
        return uri.encode()

    norm = await normaliser.ensure(
        key="adapt:best",
        login="adapt",
        workdir=tmp_path / "adapt",
        width=1920,
        height=1080,
        fps=60,
        fetch_segment=fetch,
        launch=launch,
    )
    assert norm is not None
    normaliser.submit(norm, ["good.ts", "bad.ts", "later.ts"])
    for _ in range(40):
        if len(process.stdin.written) == 2:
            break
        await asyncio.sleep(0.01)

    assert process.stdin.written == [b"good.ts", b"later.ts"]
    assert norm.stats.fetch_failures == 1


# -------------------------------------------------------------- failure policy
async def test_a_dead_encoder_is_restarted(tmp_path: Path, stopped):
    first, second = FakeProcess(), FakeProcess()
    launch, seen = make_launcher([first, second])

    async def fetch(uri: str) -> bytes:
        return b""

    kwargs = {
        "key": "adapt:best",
        "login": "adapt",
        "workdir": tmp_path / "adapt",
        "width": 1920,
        "height": 1080,
        "fps": 60,
        "fetch_segment": fetch,
        "launch": launch,
    }
    norm = await normaliser.ensure(**kwargs)
    assert norm is not None
    first.returncode = 1

    norm = await normaliser.ensure(**kwargs)
    assert norm is not None
    assert len(seen) == 2, "the encoder was not restarted"
    assert norm.stats.restarts == 1


async def test_an_encoder_that_will_not_stay_up_gives_up(tmp_path: Path, stopped):
    """Falling back to the upstream playlist beats burning CPU on a dead loop.

    Repeated instant exits mean the invocation itself is wrong - a codec the
    host does not have, a device that is not there - and no number of restarts
    fixes that. Returning None tells the caller to serve upstream directly, so
    the channel still plays.
    """
    processes = [FakeProcess() for _ in range(6)]
    launch, _ = make_launcher(processes)

    async def fetch(uri: str) -> bytes:
        return b""

    kwargs = {
        "key": "adapt:best",
        "login": "adapt",
        "workdir": tmp_path / "adapt",
        "width": 1920,
        "height": 1080,
        "fps": 60,
        "fetch_segment": fetch,
        "launch": launch,
    }
    result = None
    for process in processes:
        result = await normaliser.ensure(**kwargs)
        if result is None:
            break
        process.returncode = 1

    assert result is None, "it should have stopped trying"


async def test_a_playlist_with_no_segments_is_not_ready(tmp_path: Path, stopped):
    """Serving an empty encoder output would fail the probe on the client."""
    process = FakeProcess()
    launch, _ = make_launcher([process])

    async def fetch(uri: str) -> bytes:
        return b""

    workdir = tmp_path / "adapt"
    norm = await normaliser.ensure(
        key="adapt:best",
        login="adapt",
        workdir=workdir,
        width=1920,
        height=1080,
        fps=60,
        fetch_segment=fetch,
        launch=launch,
    )
    assert norm is not None
    assert not norm.ready(), "no playlist has been written yet"

    norm.playlist_path.write_text("#EXTM3U\n", encoding="utf-8")
    assert not norm.ready(), "a playlist without segments is not playable"

    (workdir / "seg00000.ts").write_bytes(b"x")
    (workdir / "seg00001.ts").write_bytes(b"x")
    assert norm.ready()


async def test_stopping_removes_the_work_directory(tmp_path: Path):
    process = FakeProcess()
    launch, _ = make_launcher([process])

    async def fetch(uri: str) -> bytes:
        return b""

    workdir = tmp_path / "adapt"
    norm = await normaliser.ensure(
        key="adapt:best",
        login="adapt",
        workdir=workdir,
        width=1920,
        height=1080,
        fps=60,
        fetch_segment=fetch,
        launch=launch,
    )
    assert norm is not None
    (workdir / "seg00000.ts").write_bytes(b"x")

    await normaliser.stop("adapt:best")

    assert process.terminated
    assert not workdir.exists(), "a stopped encoder must not leave its video behind"
    assert normaliser.get("adapt:best") is None


# ----------------------------------------------------------- hardware encoding
@pytest.mark.parametrize("accel", ["vaapi", "qsv"])
def test_a_hardware_encoder_gets_a_device_and_an_upload(tmp_path: Path, accel):
    """Naming the encoder is not enough, and getting this wrong looks like a hang.

    The scale/pad filters run on the CPU. Handing their software frames straight
    to a hardware encoder makes ffmpeg exit during setup, so the stream never
    starts - the encoder needs a device opened before the input and the frames
    uploaded at the end of the filter chain.
    """
    argv = normaliser.build_command(
        workdir=tmp_path, width=1920, height=1080, fps=60, hwaccel=accel
    )
    joined = " ".join(argv)
    assert normaliser.VAAPI_DEVICE in joined, "no render device was opened"

    vf = argv[argv.index("-vf") + 1]
    assert vf.endswith("format=nv12,hwupload"), (
        "frames must be uploaded to the GPU as the last step of the chain"
    )
    # The device has to be set up before the input it applies to. QSV carries the
    # path inside its device string, so match on the argument containing it.
    device_at = next(i for i, a in enumerate(argv) if normaliser.VAAPI_DEVICE in a)
    assert argv.index("-i") > device_at


def test_software_encoding_uploads_nothing(tmp_path: Path):
    argv = normaliser.build_command(workdir=tmp_path, width=1920, height=1080, fps=60)
    joined = " ".join(argv)
    assert "hwupload" not in joined
    assert normaliser.VAAPI_DEVICE not in joined


def test_nvenc_takes_software_frames_directly(tmp_path: Path):
    """Unlike VAAPI and QSV, NVENC accepts CPU frames - uploading would break it."""
    argv = normaliser.build_command(
        workdir=tmp_path, width=1920, height=1080, fps=60, hwaccel="nvenc"
    )
    assert argv[argv.index("-c:v") + 1] == "h264_nvenc"
    assert "hwupload" not in " ".join(argv)


def test_the_encoder_numbering_starts_clear_of_the_passthrough(tmp_path: Path):
    """The handover from upstream playlist to encoder output must stay legal.

    The upstream playlist is served while the encoder warms up, so the swap
    replaces what the client is holding. `#EXT-X-MEDIA-SEQUENCE` may leap
    forward across that, but must never go backwards.
    """
    argv = normaliser.build_command(workdir=tmp_path, width=1920, height=1080, fps=60)
    assert int(argv[argv.index("-start_number") + 1]) >= 100_000


# --------------------------------------------------------------- diagnostics
async def test_the_encoders_own_errors_are_read_and_recorded(tmp_path: Path, stopped):
    """A piped stderr nobody reads fills up, and then the encoder blocks forever.

    That failure mode is silent and looks exactly like a stream that never
    starts, so the output has to be consumed continuously - and the text is also
    the only thing that explains why a start failed.
    """
    process = FakeProcess()
    process.stderr = FakeStderr([b"Device creation failed: -22.\n", b"Error opening output.\n"])
    launch, _ = make_launcher([process])

    async def fetch(uri: str) -> bytes:
        return b""

    norm = await normaliser.ensure(
        key="adapt:best",
        login="adapt",
        workdir=tmp_path / "adapt",
        width=1920,
        height=1080,
        fps=60,
        fetch_segment=fetch,
        launch=launch,
    )
    assert norm is not None
    for _ in range(40):
        if norm.last_error:
            break
        await asyncio.sleep(0.01)

    assert norm.last_error == "Error opening output."
    assert process.stderr.fully_read, "the pipe must be drained, or ffmpeg blocks on it"
    assert norm.snapshot()["last_error"] == "Error opening output."
