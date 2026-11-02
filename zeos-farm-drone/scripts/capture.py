"""Film the live page for the README: the stills in ``docs/`` and ``docs/farm.gif``.

Plays a recorded session back in the page, in a headless Chrome, and saves a still at
each of the moments the README walks through. The default is
``recordings/claude-morning``, a session in which Claude chose every command and
``record_morning.py`` clicked the page's controls, so what is filmed is what happened then,
step for step.
One more still comes from the case's own scripted morning, where the flight rules veto a
command, which the Claude session never gave them cause to do.

    uv run --with playwright --with pillow python scripts/capture.py [RECORDING]

Playwright drives the Chrome already installed on the machine (``channel="chrome"``);
it downloads no browser of its own.
"""

from __future__ import annotations

import io
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from PIL import Image
from playwright.sync_api import Page, sync_playwright

from zeos_farm_drone.live import Live, make_server
from zeos_farm_drone.recording import Recording

HERE = Path(__file__).resolve().parents[1]
DOCS = HERE / "docs"
RECORDING = HERE / "recordings" / "claude-morning"
PORT = 8799
SPEED = 3.0
FRAME_S = 0.35
#: Small enough that the GIF stays under the repository's 500 KB limit on added files.
GIF_WIDTH = 640
#: The smallest window the page is laid out for.
VIEW = {"width": 1440, "height": 900}

#: What the page shows at a moment the README names.
Shown = Callable[[str, str], bool]

#: Each still from the Claude session, and the moment that names it: the drone card's
#: command line, and the farmer's phone.
STILLS: dict[str, Shown] = {
    "01-vehicle-seen": lambda cmd, phone: "vehicle-check » zoom vehicle" in cmd,
    "02-battery": lambda cmd, phone: "battery-return » return" in cmd,
    "03-farmer-asks": lambda cmd, phone: "find-item » search toolbox" in cmd,
    "04-fire": lambda cmd, phone: "fire-watch » panorama" in cmd,
    "05-calves": lambda cmd, phone: (
        "livestock-check » zoom cattle" in cmd and "trough at 8%" in phone
    ),
}


def clock(page: Page) -> float:
    return float(page.inner_text("#clock").removeprefix("t = ").removesuffix("s"))


def serve(live: Live) -> Callable[[], None]:
    server = make_server(live, port=PORT)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop() -> None:
        server.shutdown()
        live.close()

    return stop


def film_claude(page: Page, recording: Path) -> list[Image.Image]:
    live = Live(source=None, replay=Recording.load(recording), speed=SPEED)
    stop = serve(live)
    frames: list[Image.Image] = []
    try:
        page.goto(f"http://127.0.0.1:{PORT}/")
        # The replay starts only once the page is up, so nothing is over before the
        # camera is running.
        live.spawn()
        page.wait_for_selector("#drones .status")
        deadline = time.monotonic() + 180
        done = False
        while time.monotonic() < deadline and not done:
            # Every trigger reads the page rather than the server, so a still shows the
            # moment it is named for and not the one before it.
            cmd, phone = page.inner_text("#drones"), page.inner_text("#phone")
            for name, shown in STILLS.items():
                if not (DOCS / f"{name}.png").exists() and shown(cmd, phone):
                    page.screenshot(path=DOCS / f"{name}.png")
            frames.append(Image.open(io.BytesIO(page.screenshot())).convert("RGB"))
            done = page.is_visible("#storycard")
            time.sleep(FRAME_S)
        page.screenshot(path=DOCS / "06-morning-done.png", full_page=True)
    finally:
        stop()
    return frames


def film_veto(page: Page) -> None:
    """The scripted morning, until the flight rules refuse a flight off the farm."""
    live = Live(source=None, playback=True, speed=SPEED)
    stop = serve(live)
    try:
        page.goto(f"http://127.0.0.1:{PORT}/")
        live.spawn()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if "VETO" in page.inner_text("#rulings") and clock(page) > 0:
                page.locator("#rulings").scroll_into_view_if_needed()
                time.sleep(0.4)
                page.screenshot(path=DOCS / "07-flight-rules.png", full_page=True)
                return
            time.sleep(0.2)
        raise SystemExit("the scripted morning never showed a veto")
    finally:
        stop()


def save_gif(frames: list[Image.Image]) -> None:
    small = [f.resize((GIF_WIDTH, round(f.height * GIF_WIDTH / f.width))) for f in frames]
    palette = small[len(small) // 2].quantize(colors=64, method=Image.Quantize.MEDIANCUT)
    gif = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in small]
    durations = [int(FRAME_S * 1000)] * (len(gif) - 1) + [3000]
    gif[0].save(
        DOCS / "farm.gif",
        save_all=True,
        append_images=gif[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )


def main() -> None:
    recording = Path(sys.argv[1]) if len(sys.argv) > 1 else RECORDING
    DOCS.mkdir(exist_ok=True)
    for old in DOCS.glob("*.png"):
        old.unlink()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport=VIEW)
        page.emulate_media(color_scheme="light")
        frames = film_claude(page, recording)
        film_veto(page)
        browser.close()
    save_gif(frames)
    missing = sorted(n for n in STILLS if not (DOCS / f"{n}.png").exists())
    size = (DOCS / "farm.gif").stat().st_size // 1024
    print(f"{len(frames)} frames from {recording}; farm.gif {size} KB")
    if missing:
        raise SystemExit(f"never saw the moments for {missing}")


if __name__ == "__main__":
    main()
