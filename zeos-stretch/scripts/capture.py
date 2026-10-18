"""Film the live page for the README: the stills in ``docs/`` and ``docs/atrium.gif``.

Plays a recorded session back in the page, in a headless Chrome, and saves a still at
each of the moments the README walks through. The default is
``recordings/claude-morning``, a session in which Claude chose every command and a person
clicked the page's buttons, so what is filmed is what happened then, step for step.

    uv run --with playwright --with pillow python scripts/capture.py [RECORDING]

Playwright drives the Chrome already installed on the machine (``channel="chrome"``);
it downloads no browser of its own.
"""

from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import Page, sync_playwright

from zeos_stretch.live import Live, make_server
from zeos_stretch.recording import Recording

HERE = Path(__file__).resolve().parents[1]
DOCS = HERE / "docs"
RECORDING = HERE / "recordings" / "claude-morning"
PORT = 8799
SPEED = 3.0
FRAME_S = 0.35
#: The first coffee's whole story: ordered, an alarm while tidying, an alarm while the
#: coffee is carried, the detour back for it, delivered. Filming stops here.
UNTIL_S = 46.0
#: Small enough that the GIF stays under the repository's 500 KB limit on added files.
GIF_WIDTH = 760

#: Each still, and the command on the page that names its moment.
STILLS = {
    "01-coffee-ordered": "brew-coffee » navigate coffee-machine",
    "02-alarm-while-tidying": "smoke-response » navigate hotplate",
    "03-alarm-mid-delivery": "smoke-response » place coffee",
    "04-resume": "deliver-coffee » navigate corridor",
    "05-delivered": "deliver-coffee » place coffee",
}


def clock(page: Page) -> float:
    return float(page.inner_text("#clock").removeprefix("t = ").removesuffix("s"))


def main() -> None:
    recording = Path(sys.argv[1]) if len(sys.argv) > 1 else RECORDING
    DOCS.mkdir(exist_ok=True)
    live = Live(source=None, replay=Recording.load(recording), speed=SPEED)
    server = make_server(live, port=PORT)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    frames: list[Image.Image] = []
    stills: dict[str, bytes] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1320, "height": 860})
        page.emulate_media(color_scheme="light")
        page.goto(f"http://127.0.0.1:{PORT}/")
        # The replay starts only once the page is up, so nothing is over before the
        # camera is running.
        live.spawn()
        page.wait_for_selector("#jobs tr")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and clock(page) < UNTIL_S:
            # Every trigger reads the page rather than the server, so a still shows the
            # moment it is named for and not the one before it.
            shows = page.inner_text("#cmd")
            for name, command in STILLS.items():
                if name not in stills and command in shows:
                    full = name == "04-resume"  # the resume notice is further down the page
                    stills[name] = page.screenshot(full_page=full)
            frames.append(Image.open(io.BytesIO(page.screenshot())).convert("RGB"))
            time.sleep(FRAME_S)
        browser.close()
    server.shutdown()
    live.close()

    for name, png in stills.items():
        (DOCS / f"{name}.png").write_bytes(png)
    small = [f.resize((GIF_WIDTH, round(f.height * GIF_WIDTH / f.width))) for f in frames]
    palette = small[len(small) // 2].quantize(colors=64, method=Image.Quantize.MEDIANCUT)
    gif = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in small]
    durations = [int(FRAME_S * 1000)] * (len(gif) - 1) + [2500]
    gif[0].save(
        DOCS / "atrium.gif",
        save_all=True,
        append_images=gif[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    missing = sorted(set(STILLS) - set(stills))
    print(f"{len(frames)} frames from {recording}; stills: {sorted(stills)}")
    if missing:
        raise SystemExit(f"never saw the moments for {missing}")


if __name__ == "__main__":
    main()
