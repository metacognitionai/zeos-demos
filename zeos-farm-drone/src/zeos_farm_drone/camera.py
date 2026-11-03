"""The drone's own camera in Gazebo: every photo an alert cites is a real frame.

PX4's ``x500_gimbal`` carries a camera on a gimbal. When a look begins, ``Px4Farm``
has PX4 point the gimbal at what the look is about (gimbal angles over MAVLink, as
on a real airframe); when the look is done and the farm files a photo,
``Camera`` keeps the frame the camera sees then, named by the photo's id. Between
photos it keeps a frame every ``every_s`` seconds as ``live.png``, for the page.

What a photo shows is scenery: the answer a look gives the kernel still comes from
the farm's ground truth, never from the picture.

Frames are taken by ``gz_snap.py`` under the system Python, which has Gazebo's
bindings. The kernel's thread never waits for one: ``snap`` queues the name, and a
thread of its own takes it. Gazebo renders nothing while it loads what it is asked
to show -- the herds of a new day into a fresh world -- which can outlast one
snap's wait, so a frame Gazebo would not give is asked for again. Only when no frame
has come for ``patience_s`` does the next ``check`` raise.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

__all__ = ["LIVE", "SNAP", "Camera", "image_topic"]

SNAP = Path(__file__).with_name("gz_snap.py")
#: The frame the page shows as what the drone sees now.
LIVE = "live"


def image_topic(world: str, model: str) -> str:
    """Where the gimbal camera of ``model`` publishes in ``world``."""
    return f"/world/{world}/model/{model}/link/camera_link/sensor/camera/image"


def _snap(topic: str, out: Path) -> None:
    done = subprocess.run(
        ["/usr/bin/python3", "-I", str(SNAP), topic, str(out)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if done.returncode != 0:
        raise RuntimeError(f"no frame from {topic} (exit {done.returncode}): {done.stderr.strip()}")


class Camera:
    """Frames from ``topic``, written to ``out_dir`` as ``<name>.png``."""

    def __init__(
        self,
        topic: str,
        out_dir: Path,
        *,
        every_s: float = 2.0,
        patience_s: float = 120.0,
        snap: Callable[[str, Path], None] = _snap,
    ) -> None:
        self.topic = topic
        self.out_dir = out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        self._every_s = every_s
        self._patience_s = patience_s
        self._last_frame = time.monotonic()
        self._snap = snap
        self._wake = threading.Condition()
        self._queued: deque[str] = deque()
        self._failed: BaseException | None = None
        self._stopping = False
        self._thread = threading.Thread(target=self._loop, name="camera", daemon=True)
        self._thread.start()

    def file_into(self, out_dir: Path) -> None:
        """Write frames to ``out_dir`` from now on: a new morning keeps its own."""
        out_dir.mkdir(parents=True, exist_ok=True)
        with self._wake:
            self.out_dir = out_dir

    def snap(self, name: str) -> None:
        """Keep the next frame as ``<name>.png``."""
        self.check()
        with self._wake:
            self._queued.append(name)
            self._wake.notify()

    def check(self) -> None:
        with self._wake:
            failed = self._failed
        if failed is not None:
            raise failed

    def close(self) -> None:
        """Take the photos already asked for, then stop."""
        with self._wake:
            self._stopping = True
            self._wake.notify()
        self._thread.join(60)

    def _loop(self) -> None:
        # The first live frame at once, however long the clock has run.
        last_live = float("-inf")
        while True:
            with self._wake:
                while not self._queued and not self._stopping:
                    left = last_live + self._every_s - time.monotonic()
                    if left <= 0:
                        break
                    self._wake.wait(left)
                if self._queued:
                    name = self._queued.popleft()
                elif self._stopping:
                    return
                else:
                    name = LIVE
            try:
                self._take(name)
            except BaseException as error:  # noqa: BLE001 -- handed to the kernel's thread
                with self._wake:
                    self._failed = error
                return
            if name == LIVE:
                last_live = time.monotonic()

    def _take(self, name: str) -> None:
        # Written aside and renamed, so the page never reads half a picture.
        with self._wake:
            out_dir = self.out_dir
        part = out_dir / f".{name}.png"
        while True:
            try:
                self._snap(self.topic, part)
                break
            except Exception:
                if time.monotonic() - self._last_frame >= self._patience_s:
                    raise
                time.sleep(1)  # a snap that fails at once must not spin
        self._last_frame = time.monotonic()
        part.replace(out_dir / f"{name}.png")
