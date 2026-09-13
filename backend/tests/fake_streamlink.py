"""A stand-in for `streamlink --stdout`, for the live stream tests.

Usage: fake_streamlink.py <mode> [<packets>]

  stream   write <packets> TS packets in a few bursts, then exit 0
  forever  write TS packets until killed
  offline  print streamlink's offline error to stderr and exit 1
  fail     print an unrelated error to stderr and exit 1
  silent   produce nothing and never exit (startup timeout)
  chatty   flood stderr, then stream <packets> and exit (full-pipe check)

Modes that behave differently on a restart take a state file as a third
argument; the first run creates it, later runs see it:

  once-then-offline  stream <packets> once, then report the channel offline
  once-then-fail     stream <packets> once, then fail to start
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PACKET = bytes([0x47]) + bytes(187)


def write_packets(count: int) -> None:
    out = sys.stdout.buffer
    for start in range(0, count, 50):
        out.write(PACKET * min(50, count - start))
        out.flush()
        time.sleep(0.01)


def main() -> None:
    mode = sys.argv[1]
    packets = int(sys.argv[2]) if len(sys.argv) > 2 else 200

    if mode == "stream":
        write_packets(packets)
    elif mode == "forever":
        while True:
            write_packets(50)
    elif mode == "offline":
        sys.stderr.write("[cli][info] Found matching plugin twitch for URL\n")
        sys.stderr.write("error: No playable streams found on this URL: https://www.twitch.tv/x\n")
        sys.stderr.flush()
        sys.exit(1)
    elif mode == "fail":
        sys.stderr.write("error: Unable to open URL: 403 Client Error\n")
        sys.stderr.flush()
        sys.exit(1)
    elif mode == "silent":
        time.sleep(3600)
    elif mode in ("once-then-offline", "once-then-fail"):
        state = Path(sys.argv[3])
        if not state.exists():
            state.write_text("ran")
            write_packets(packets)
        elif mode == "once-then-offline":
            sys.stderr.write("error: No playable streams found on this URL: https://www.twitch.tv/x\n")
            sys.stderr.flush()
            sys.exit(1)
        else:
            sys.stderr.write("error: Unable to open URL: 500 Server Error\n")
            sys.stderr.flush()
            sys.exit(1)
    elif mode == "chatty":
        # Well past a pipe buffer (64 KiB on Linux, 4 KiB on some platforms).
        for i in range(4000):
            sys.stderr.write(f"[stream.hls][debug] segment {i} downloaded ok, padding padding\n")
        sys.stderr.flush()
        write_packets(packets)
    else:
        sys.exit(f"unknown mode {mode}")


main()
