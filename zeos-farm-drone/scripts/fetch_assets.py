# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow==13.0.0"]
# ///
"""Download the models and textures the Gazebo farm is dressed with.

    uv run scripts/fetch_assets.py [sim/models]

Models come from Gazebo Fuel and textures from ambientCG, each at a pinned version,
into a resource directory Gazebo searches (``GZ_SIM_RESOURCE_PATH``): a model as
``model://NAME``, a texture as ``model://farm_textures/ID/ID_Color.jpg``: every file
has a name of its own, since Ogre keeps loaded textures by file name alone. Nothing here is
kept in the repository; ``ATTRIBUTION.md`` beside them says where each came from and
under which licence.

What is committed is ``sim/models.sha256``: the SHA-256 of every file the farm was
built and filmed with. Once everything is in place, each file is checked against it,
so a farm dressed by this script is the farm in the pictures, byte for byte, or the
script says which file is not. A file already in place is checked, not fetched again.
After changing what is fetched, ``--record`` writes the list anew from what is there.

A ground texture laid over a whole paddock repeats hundreds of times, and the slight
light and shade across one photograph then shows as a grid. Each ground texture's
colour map is flattened: divided by a heavy blur of itself, wrapped as it tiles, so the
grass and grit stay and the grid goes. Pillow is pinned because the flattened files are
its output, and are checked like the rest.
"""

from __future__ import annotations

import hashlib
import io
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

FUEL = "https://fuel.gazebosim.org/1.0"
#: Directory name: (owner, Fuel name, version, licence). A model that names its own
#: files as ``model://ITS NAME/...`` must live in a directory of that name.
MODELS: dict[str, tuple[str, str, int, str]] = {
    "pickup": ("OpenRobotics", "Pickup", 4, "CC0 1.0"),
    "TruckBox": ("OpenRobotics", "TruckBox", 2, "CC BY 4.0"),
    "hatchback_red": ("OpenRobotics", "Hatchback red", 4, "CC0 1.0"),
    "house_1": ("OpenRobotics", "House 1", 3, "CC0 1.0"),
    "water_tower": ("OpenRobotics", "Water tower", 3, "CC0 1.0"),
    "Oak tree": ("OpenRobotics", "Oak tree", 7, "CC0 1.0"),
    "Pine Tree": ("OpenRobotics", "Pine Tree", 6, "CC0 1.0"),
    "smoke": ("chapulina", "Smoke", 1, "CC BY 4.0"),
}
#: ambientCG material IDs, all CC0 1.0, at 1K resolution.
TEXTURES = ("Grass004", "Ground037", "Ground081", "Ground103", "CorrugatedSteel005")
#: The textures laid over open ground, whose colour is flattened.
GROUND = ("Grass004", "Ground037", "Ground081", "Ground103")
#: The maps a PBR material here uses, by ambientCG's file suffix.
MAPS = ("Color", "NormalGL", "Roughness")
#: Every file under the resource directory and its SHA-256, as ``sha256sum`` writes them.
MANIFEST = Path(__file__).resolve().parents[1] / "sim" / "models.sha256"


def get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "zeos-farm-drone"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def fetch_model(root: Path, name: str, owner: str, model: str, version: int) -> None:
    target = root / name
    if (target / "model.sdf").exists():
        return
    quoted = urllib.parse.quote(model)
    archive = zipfile.ZipFile(
        io.BytesIO(get(f"{FUEL}/{owner}/models/{quoted}/{version}/{quoted}.zip"))
    )
    target.mkdir(parents=True, exist_ok=True)
    archive.extractall(target)


def fetch_texture(root: Path, material: str) -> None:
    target = root / "farm_textures" / material
    if all((target / f"{material}_{m}.jpg").exists() for m in MAPS):
        return
    archive = zipfile.ZipFile(
        io.BytesIO(get(f"https://ambientcg.com/get?file={material}_1K-JPG.zip"))
    )
    target.mkdir(parents=True, exist_ok=True)
    for m in MAPS:
        (target / f"{material}_{m}.jpg").write_bytes(archive.read(f"{material}_1K-JPG_{m}.jpg"))
    if material in GROUND:
        flatten(target / f"{material}_Color.jpg")


def flatten(path: Path) -> None:
    """Take the light and shade across the whole photograph out of a tiling texture:
    each channel times its mean, over a heavy blur of itself."""
    from PIL import Image, ImageFilter, ImageStat

    image = Image.open(path).convert("RGB")
    w, h = image.size
    # Blur three-by-three copies and keep the middle: the blur wraps as the tile does.
    tiled = Image.new("RGB", (w * 3, h * 3))
    for i in range(3):
        for j in range(3):
            tiled.paste(image, (i * w, j * h))
    light = tiled.filter(ImageFilter.GaussianBlur(w / 6)).crop((w, h, 2 * w, 2 * h))
    bands = []
    for detail, shade, mean in zip(
        image.split(), light.split(), ImageStat.Stat(image).mean, strict=True
    ):
        pixels = zip(detail.tobytes(), shade.tobytes(), strict=True)
        flat = bytes(min(255, round(d * mean / max(s, 1))) for d, s in pixels)
        bands.append(Image.frombytes("L", image.size, flat))
    Image.merge("RGB", bands).save(path, quality=92)


def attribution() -> str:
    lines = [
        "# Where the farm's models and textures come from",
        "",
        "Downloaded by `scripts/fetch_assets.py`; not part of the repository.",
        "",
        "| Directory | Source | Licence |",
        "|---|---|---|",
    ]
    for name, (owner, model, version, licence) in MODELS.items():
        url = f"{FUEL}/{owner}/models/{urllib.parse.quote(model)}/{version}"
        lines.append(
            f"| `{name}` | [{owner}/{model}, version {version}]({url}) on Gazebo Fuel | {licence} |"
        )
    for material in TEXTURES:
        lines.append(
            f"| `farm_textures/{material}` | [{material}](https://ambientcg.com/a/{material}) "
            "on ambientCG | CC0 1.0 |"
        )
    return "\n".join(lines) + "\n"


def digests(root: Path) -> dict[str, str]:
    """Every file under ``root``, by its path there, and its SHA-256."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.startswith(".")
    }


def write_manifest(found: dict[str, str]) -> None:
    MANIFEST.write_text("".join(f"{digest}  {name}\n" for name, digest in sorted(found.items())))


def check(found: dict[str, str]) -> list[str]:
    """What differs from the manifest, one line each; nothing when all of it matches."""
    wanted = {
        name: digest
        for digest, name in (line.split("  ", 1) for line in MANIFEST.read_text().splitlines())
    }
    problems = [f"missing  {n}" for n in sorted(wanted.keys() - found.keys())]
    problems += [f"extra    {n}" for n in sorted(found.keys() - wanted.keys())]
    problems += [
        f"differs  {n}" for n in sorted(wanted.keys() & found.keys()) if wanted[n] != found[n]
    ]
    return problems


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--record"]
    root = Path(args[0] if args else "sim/models")
    for name, (owner, model, version, _) in MODELS.items():
        fetch_model(root, name, owner, model, version)
        print(f"model    {name}")
    for material in TEXTURES:
        fetch_texture(root, material)
        print(f"texture  {material}")
    (root / "ATTRIBUTION.md").write_text(attribution())
    found = digests(root)
    if "--record" in sys.argv:
        write_manifest(found)
        print(f"recorded {len(found)} files in {MANIFEST}")
        return
    problems = check(found)
    if problems:
        sys.exit(f"{root} is not the farm {MANIFEST.name} records:\n  " + "\n  ".join(problems))
    print(f"checked  {len(found)} files against {MANIFEST.name}: all as recorded")


if __name__ == "__main__":
    main()
