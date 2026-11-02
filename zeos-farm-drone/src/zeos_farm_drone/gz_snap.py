"""Take one frame from a Gazebo camera and write it as a PNG.

    python3 gz_snap.py TOPIC OUT.png [TIMEOUT_S]

Run by the system Python that ships with Gazebo (``python3-gz-transport13``), not by
the demo's environment: the gz bindings are not on PyPI. Hence the standard library
only, besides them, and no import from the rest of the package. A Gazebo camera
renders only while something subscribes to it, so this subscribes, keeps the first
frame that arrives, and leaves.
"""

from __future__ import annotations

import os
import struct
import sys
import threading
import zlib
from pathlib import Path

from gz.msgs10.image_pb2 import Image  # pyright: ignore[reportMissingImports]
from gz.transport13 import Node  # pyright: ignore[reportMissingImports]

RGB_INT8 = 3


def png(width: int, height: int, rgb: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    stride = width * 3
    rows = b"".join(b"\x00" + rgb[y * stride : (y + 1) * stride] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    topic, out = sys.argv[1], Path(sys.argv[2])
    timeout_s = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
    got = threading.Event()
    frame: list[Image] = []

    def keep(msg: Image) -> None:
        if not frame:
            frame.append(msg)
            got.set()

    node = Node()
    if not node.subscribe(Image, topic, keep):
        print(f"cannot subscribe to {topic}", file=sys.stderr)
        return 2
    if not got.wait(timeout_s):
        print(f"no frame on {topic} within {timeout_s:.0f} s", file=sys.stderr)
        return 1
    image = frame[0]
    if image.pixel_format_type != RGB_INT8:
        print(f"frames on {topic} are not RGB8 ({image.pixel_format_type})", file=sys.stderr)
        return 3
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png(image.width, image.height, image.data))
    node.unsubscribe(topic)
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Leave without the interpreter's teardown: destroying the node while
    # gz-transport's threads still run aborts the process now and then
    # ("terminate called without an active exception"), after the frame is written.
    os._exit(code)
