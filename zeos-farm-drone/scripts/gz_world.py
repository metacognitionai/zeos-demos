"""Write the farm as a Gazebo world, for PX4 SITL to fly over.

    uv run python scripts/gz_world.py sim

writes ``sim/farm.sdf`` and the models only this farm has, in ``sim/farm_models``.

The world is built from the same map the demo plans on (``farm.py``) and placed with
the same origin (``geo.py``): the drone's landing pad, beside the shed, is the world
origin, Gazebo's x is east and y is north, and every paddock is where ``geo.at`` sends
the drone. This is what never changes; what does -- herds, water, vehicles, smoke --
``zeos_farm_drone.scene`` adds while the farm runs, from the models written here. Nothing
in it is read back: what the drone sees is still decided by the farm's ground truth.

Downloaded models and textures (``scripts/fetch_assets.py``) are found as ``model://``
in ``sim/models``; everything else is drawn here, from primitives and small meshes whose
texture coordinates are in metres, so a texture tiles at its real size.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

from zeos_farm_drone.farm import PADDOCKS
from zeos_farm_drone.geo import ORIGIN, UNIT_M
from zeos_farm_drone.scene import PADDOCK_M, creek_x, trough_at, xy

#: One generator, one farm: the same seed places the same trees.
SEED = 7
TEX = "model://farm_textures"


# -- small SDF helpers ---------------------------------------------------------------


def pbr(
    rgb: str,
    *,
    texture: str | None = None,
    roughness: float = 0.9,
    metalness: float = 0.0,
    emissive: str | None = None,
) -> str:
    """A material: ``texture`` names an ambientCG folder in ``farm_textures``, whose
    colour map ``rgb`` tints. Its files are named for it (``Grass004_Color.jpg``):
    Ogre keeps a loaded texture by file name alone, so two ``Color.jpg`` would be one."""
    maps = ""
    if texture is not None:
        maps = (
            f"<albedo_map>{TEX}/{texture}/{texture}_Color.jpg</albedo_map>"
            f"<normal_map>{TEX}/{texture}/{texture}_NormalGL.jpg</normal_map>"
            f"<roughness_map>{TEX}/{texture}/{texture}_Roughness.jpg</roughness_map>"
        )
    glow = f"<emissive>{emissive} 1</emissive>" if emissive else ""
    return (
        f"<material><ambient>{rgb} 1</ambient><diffuse>{rgb} 1</diffuse>"
        f"<specular>0.1 0.1 0.1 1</specular>{glow}"
        f"<pbr><metal>{maps}<roughness>{roughness}</roughness>"
        f"<metalness>{metalness}</metalness></metal></pbr></material>"
    )


def visual(
    name: str, geometry: str, material: str, pose: str = "0 0 0 0 0 0", shadows: bool = True
) -> str:
    cast = "" if shadows else "<cast_shadows>false</cast_shadows>"
    return (
        f'<visual name="{name}"><pose>{pose}</pose>{cast}'
        f"<geometry>{geometry}</geometry>{material}</visual>"
    )


def box(sx: float, sy: float, sz: float) -> str:
    return f"<box><size>{sx:g} {sy:g} {sz:g}</size></box>"


def cylinder(radius: float, length: float) -> str:
    return f"<cylinder><radius>{radius:g}</radius><length>{length:g}</length></cylinder>"


def ellipsoid(rx: float, ry: float, rz: float) -> str:
    return f"<ellipsoid><radii>{rx:g} {ry:g} {rz:g}</radii></ellipsoid>"


def mesh(uri: str) -> str:
    return f"<mesh><uri>{uri}</uri></mesh>"


def static(name: str, pose: str, *visuals: str, collide: str = "") -> str:
    return (
        f'<model name="{name}"><static>true</static><pose>{pose}</pose>'
        f'<link name="link">{collide}{"".join(visuals)}</link></model>'
    )


def include(name: str, uri: str, pose: str) -> str:
    return f"<include><name>{name}</name><uri>{uri}</uri><pose>{pose}</pose></include>"


# -- meshes with texture coordinates in metres ---------------------------------------


def quad_obj(width: float, depth: float, tile_m: float) -> str:
    """A flat rectangle centred on the origin, its texture repeating every ``tile_m``."""
    w, d, u, v = width / 2, depth / 2, width / tile_m, depth / tile_m
    return (
        f"v {-w} {-d} 0\nv {w} {-d} 0\nv {w} {d} 0\nv {-w} {d} 0\n"
        f"vt 0 0\nvt {u} 0\nvt {u} {v}\nvt 0 {v}\nvn 0 0 1\n"
        "f 1/1/1 2/2/1 3/3/1\nf 1/1/1 3/3/1 4/4/1\n"
    )


def strip_obj(points: list[tuple[float, float]], width: float, tile_m: float) -> str:
    """A ribbon ``width`` wide along ``points``, flat on the ground."""
    lines: list[str] = []
    along = 0.0
    for i, (x, y) in enumerate(points):
        nx, ny = points[min(i + 1, len(points) - 1)]
        px, py = points[max(i - 1, 0)]
        dx, dy = nx - px, ny - py
        norm = math.hypot(dx, dy) or 1.0
        ox, oy = -dy / norm * width / 2, dx / norm * width / 2
        if i:
            along += math.dist(points[i - 1], (x, y))
        lines.append(f"v {x - ox:.3f} {y - oy:.3f} 0\nv {x + ox:.3f} {y + oy:.3f} 0")
        lines.append(f"vt 0 {along / tile_m:.4f}\nvt {width / tile_m:.4f} {along / tile_m:.4f}")
    lines.append("vn 0 0 1")
    for i in range(len(points) - 1):
        a, b, c, d = 2 * i + 1, 2 * i + 2, 2 * i + 4, 2 * i + 3
        # Counter-clockwise seen from above, or the face looks at the ground.
        lines.append(f"f {a}/{a}/1 {d}/{d}/1 {c}/{c}/1\nf {a}/{a}/1 {c}/{c}/1 {b}/{b}/1")
    return "\n".join(lines) + "\n"


def disc_obj(radius: float, tile_m: float, segments: int = 48) -> str:
    lines = ["v 0 0 0", f"vt {radius / tile_m:.4f} {radius / tile_m:.4f}"]
    for i in range(segments):
        a = 2 * math.pi * i / segments
        x, y = radius * math.cos(a), radius * math.sin(a)
        lines.append(f"v {x:.3f} {y:.3f} 0")
        lines.append(f"vt {(x + radius) / tile_m:.4f} {(y + radius) / tile_m:.4f}")
    lines.append("vn 0 0 1")
    for i in range(segments):
        b, c = i + 2, (i + 1) % segments + 2
        lines.append(f"f 1/1/1 {b}/{b}/1 {c}/{c}/1")
    return "\n".join(lines) + "\n"


def shed_obj(length: float, width: float, wall: float, ridge: float, tile_m: float) -> str:
    """A gabled shed: four walls and a two-sided roof, texture in metres along each face."""
    hl, hw = length / 2, width / 2
    faces: list[tuple[list[tuple[float, float, float]], list[tuple[float, float]]]] = []

    def wall_face(
        a: tuple[float, float], b: tuple[float, float], top_a: float, top_b: float
    ) -> None:
        span = math.dist(a, b) / tile_m
        faces.append(
            (
                [(a[0], a[1], 0), (b[0], b[1], 0), (b[0], b[1], top_b), (a[0], a[1], top_a)],
                [(0, 0), (span, 0), (span, top_b / tile_m), (0, top_a / tile_m)],
            )
        )

    wall_face((-hl, -hw), (hl, -hw), wall, wall)
    wall_face((hl, hw), (-hl, hw), wall, wall)
    # The gable ends: a wall with the roof's triangle above it.
    for x, sign in ((hl, 1), (-hl, -1)):
        wall_face((x, -hw * sign), (x, hw * sign), wall, wall)
        faces.append(
            (
                [(x, -hw * sign, wall), (x, hw * sign, wall), (x, 0, ridge)],
                [(0, 0), (width / tile_m, 0), (width / 2 / tile_m, (ridge - wall) / tile_m)],
            )
        )
    slope = math.hypot(hw + 0.6, ridge - wall + 0.3) / tile_m
    for side in (-1, 1):
        eave = side * (hw + 0.6)
        faces.append(
            (
                [
                    (-hl - 0.4, eave, wall - 0.3),
                    (hl + 0.4, eave, wall - 0.3),
                    (hl + 0.4, 0, ridge),
                    (-hl - 0.4, 0, ridge),
                ],
                [
                    (0, 0),
                    ((length + 0.8) / tile_m, 0),
                    ((length + 0.8) / tile_m, slope),
                    (0, slope),
                ],
            )
        )
    lines: list[str] = []
    n = 0
    for corners, uvs in faces:
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = corners[:3]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        norm = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        lines.append(f"vn {nx / norm:.4f} {ny / norm:.4f} {nz / norm:.4f}")
        normal = len([line for line in lines if line.startswith("vn ")])
        first = n + 1
        for (x, y, z), (u, v) in zip(corners, uvs, strict=True):
            lines.append(f"v {x:.3f} {y:.3f} {z:.3f}\nvt {u:.4f} {v:.4f}")
            n += 1
        for k in range(1, len(corners) - 1):
            i, j, m = first, first + k, first + k + 1
            lines.append(f"f {i}/{i}/{normal} {j}/{j}/{normal} {m}/{m}/{normal}")
    return "\n".join(lines) + "\n"


# -- the farm's own models, used by the scene as props -------------------------------

#: Hereford reds, from a dark bull-red to a faded summer coat.
HEREFORD_REDS = ("0.36 0.12 0.05", "0.42 0.17 0.08", "0.50 0.23 0.11")
HEREFORD_WHITE = "0.84 0.80 0.72"
HOOF = "0.12 0.10 0.09"
#: Variants of a cow: each red, head down grazing or head up.
COW_VARIANTS = [(red, grazing) for red in HEREFORD_REDS for grazing in (True, False)]


def cattle_sdf(name: str, s: float, red_rgb: str, grazing: bool) -> str:
    """A Hereford, the beef breed of the Riverina: a deep red body, white face, brisket
    and socks. ``s`` scales it (a calf is about half a cow); a grazing one has its head
    down in the grass."""
    red, white = pbr(red_rgb, roughness=0.85), pbr(HEREFORD_WHITE, roughness=0.8)
    dark, pink = pbr(HOOF, roughness=0.6), pbr("0.62 0.42 0.38", roughness=0.7)

    def part(
        label: str, shape: str, material: str, x: float, y: float, z: float, pitch: float = 0
    ) -> str:
        return visual(
            label, shape, material, f"{x * s:.3f} {y * s:.3f} {z * s:.3f} 0 {pitch:.3f} 0"
        )

    # Head and neck: down to the grass, or up and looking about.
    if grazing:
        neck, head, nose, ears = (
            (0.95, 0.88, 0.75),
            (1.22, 0.55, 1.15),
            (1.36, 0.30, 1.15),
            (1.12, 0.72),
        )
    else:
        neck, head, nose, ears = (
            (0.95, 1.18, -0.35),
            (1.25, 1.22, 0.35),
            (1.46, 1.10, 0.35),
            (1.15, 1.36),
        )
    parts = [
        part("barrel", ellipsoid(0.78, 0.45, 0.46), red, 0.02, 0, 0.98),
        part("rump", ellipsoid(0.52, 0.43, 0.45), red, -0.58, 0, 1.02),
        part("shoulders", ellipsoid(0.44, 0.40, 0.44), red, 0.52, 0, 1.06),
        part("brisket", ellipsoid(0.34, 0.30, 0.30), white, 0.70, 0, 0.78),
        part("belly", ellipsoid(0.55, 0.30, 0.16), white, 0.10, 0, 0.62),
        part("neck", ellipsoid(0.36, 0.25, 0.28), red, neck[0], 0, neck[1], neck[2]),
        part("head", ellipsoid(0.30, 0.17, 0.18), white, head[0], 0, head[1], head[2]),
        part("muzzle", ellipsoid(0.10, 0.12, 0.09), pink, nose[0], 0, nose[1], nose[2]),
        part("ear-l", ellipsoid(0.05, 0.14, 0.05), red, ears[0], 0.2, ears[1]),
        part("ear-r", ellipsoid(0.05, 0.14, 0.05), red, ears[0], -0.2, ears[1]),
        part("tail", cylinder(0.03, 0.75), red, -1.08, 0, 0.78, 0.15),
        part("switch", ellipsoid(0.07, 0.07, 0.13), white, -1.13, 0, 0.42),
    ]
    for leg, (lx, ly) in enumerate(((0.58, 0.24), (0.58, -0.24), (-0.62, 0.24), (-0.62, -0.24))):
        parts.append(part(f"leg{leg}", cylinder(0.09, 0.55), red, lx, ly, 0.55))
        parts.append(part(f"sock{leg}", cylinder(0.08, 0.3), white, lx, ly, 0.19))
        parts.append(part(f"hoof{leg}", cylinder(0.085, 0.06), dark, lx, ly, 0.03))
    return model_file(name, "".join(parts))


def motorbike_sdf() -> str:
    red, black = (
        pbr("0.75 0.10 0.08", roughness=0.4, metalness=0.3),
        pbr("0.05 0.05 0.05", roughness=0.7),
    )
    chrome = pbr("0.8 0.8 0.82", roughness=0.2, metalness=0.9)
    parts = [
        visual("tyre-f", cylinder(0.33, 0.12), black, "0.72 0 0.33 1.5708 0 0"),
        visual("tyre-r", cylinder(0.33, 0.14), black, "-0.72 0 0.33 1.5708 0 0"),
        visual("tank", ellipsoid(0.32, 0.16, 0.13), red, "0.15 0 0.82 0 0 0"),
        visual("seat", box(0.6, 0.24, 0.08), black, "-0.32 0 0.85 0 0 0"),
        visual("engine", box(0.4, 0.26, 0.3), chrome, "0 0 0.5 0 0 0"),
        visual("fork", cylinder(0.03, 0.75), chrome, "0.62 0 0.65 0 -0.35 0"),
        visual("bars", cylinder(0.02, 0.7), black, "0.5 0 1.0 1.5708 0 0"),
        visual("fender", box(0.5, 0.18, 0.04), red, "-0.7 0 0.7 0 0.3 0"),
    ]
    return model_file("farm_motorbike", "".join(parts))


def quad_bike_sdf() -> str:
    green, black = (
        pbr("0.18 0.42 0.16", roughness=0.5, metalness=0.2),
        pbr("0.05 0.05 0.05", roughness=0.7),
    )
    parts = [
        visual("body", box(1.5, 0.9, 0.35), green, "0 0 0.62 0 0 0"),
        visual("seat", box(0.6, 0.35, 0.12), black, "-0.15 0 0.86 0 0 0"),
        visual("rack", box(0.45, 1.0, 0.04), black, "0.55 0 0.85 0 0 0"),
        visual("bars", cylinder(0.025, 0.8), black, "0.35 0 1.05 1.5708 0 0"),
    ]
    for i, (x, y) in enumerate(((0.55, 0.5), (0.55, -0.5), (-0.55, 0.5), (-0.55, -0.5))):
        parts.append(visual(f"tyre{i}", cylinder(0.28, 0.22), black, f"{x} {y} 0.28 1.5708 0 0"))
    return model_file("farm_quad_bike", "".join(parts))


def toolbox_sdf() -> str:
    red = pbr("0.80 0.12 0.08", roughness=0.35, metalness=0.6)
    grey = pbr("0.6 0.6 0.62", roughness=0.3, metalness=0.9)
    parts = [
        visual("box", box(0.7, 0.32, 0.32), red, "0 0 0.16 0 0 0"),
        visual("lid", box(0.72, 0.34, 0.04), red, "0 0 0.34 0 0 0"),
        visual("handle", box(0.4, 0.04, 0.04), grey, "0 0 0.4 0 0 0"),
    ]
    return model_file("farm_toolbox", "".join(parts))


def smoke_sdf() -> str:
    """A bushfire on the horizon: smoke billowing from a glowing front."""
    emitter = """
      <particle_emitter name="smoke" type="box">
        <pose>0 0 6 0 -1.5708 0</pose>
        <emitting>true</emitting>
        <size>90 90 1</size>
        <particle_size>30 30 30</particle_size>
        <lifetime>60</lifetime>
        <min_velocity>6</min_velocity>
        <max_velocity>11</max_velocity>
        <scale_rate>3.5</scale_rate>
        <rate>40</rate>
        <material>
          <diffuse>0.38 0.36 0.35</diffuse>
          <pbr><metal><albedo_map>model://smoke/materials/textures/fog.png</albedo_map></metal></pbr>
        </material>
        <color_range_image>model://smoke/materials/textures/fogcolors.png</color_range_image>
      </particle_emitter>"""
    flames = "".join(
        visual(
            f"flame{i}",
            ellipsoid(14 - i * 2, 9 - i, 5 + i * 2),
            pbr("1.0 0.45 0.08", roughness=1.0, emissive="1.0 0.35 0.05"),
            f"{(i - 2) * 16} {(i % 2) * 10 - 5} {3 + i} 0 0 {i * 0.7}",
            shadows=False,
        )
        for i in range(5)
    )
    char = visual("char", cylinder(70, 0.3), pbr("0.08 0.07 0.06", roughness=1.0), "0 0 0.15 0 0 0")
    return model_file("farm_smoke", char + flames + emitter)


def model_file(name: str, link: str) -> str:
    return (
        f'<?xml version="1.0"?>\n<!-- Generated by scripts/gz_world.py; do not edit. -->\n'
        f'<sdf version="1.9"><model name="{name}"><static>true</static>'
        f'<link name="link">{link}</link></model></sdf>\n'
    )


def model_config(name: str) -> str:
    return (
        f'<?xml version="1.0"?>\n<model><name>{name}</name><version>1.0</version>'
        f'<sdf version="1.9">model.sdf</sdf><description>zeos-farm-drone: {name}'
        "</description></model>\n"
    )


# -- the world ------------------------------------------------------------------------

GROUND = "model://farm_ground/meshes"


def creek_points() -> list[tuple[float, float]]:
    """The creek's line, south to north through the creek paddock and beyond."""
    _, y0 = xy("shed")
    _, y1 = xy("north-flat")
    return [(creek_x(y), y) for y in range(int(y0 - 60), int(y1 + 140), 6)]


def fence(paddock: str) -> str:
    """Timber posts every eight metres and three plain wires, a strainer at each corner."""
    x, y = xy(paddock)
    half = PADDOCK_M / 2
    timber = pbr("0.40 0.31 0.22", roughness=0.95)
    wire = pbr("0.72 0.73 0.75", roughness=0.35, metalness=0.9)
    parts: list[str] = []
    steps = round(PADDOCK_M / 8)
    for side, (sx, sy, dx, dy) in enumerate(
        ((-half, -half, 1, 0), (half, -half, 0, 1), (half, half, -1, 0), (-half, half, 0, -1))
    ):
        for k in range(steps):
            px, py = sx + dx * PADDOCK_M * k / steps, sy + dy * PADDOCK_M * k / steps
            corner = k == 0
            radius, height = (0.12, 1.6) if corner else (0.06, 1.25)
            parts.append(
                visual(
                    f"post{side}-{k}",
                    cylinder(radius, height),
                    timber,
                    f"{px:.2f} {py:.2f} {height / 2} 0 0 0",
                )
            )
        mx, my = sx + dx * half, sy + dy * half
        yaw = math.atan2(dy, dx)
        for w, z in enumerate((0.45, 0.8, 1.15)):
            parts.append(
                visual(
                    f"wire{side}-{w}",
                    box(PADDOCK_M, 0.025, 0.025),
                    wire,
                    f"{mx:.2f} {my:.2f} {z} 0 0 {yaw:.4f}",
                    shadows=False,
                )
            )
    return static(f"fence-{paddock}", f"{x} {y} 0 0 0 0", *parts)


def trough(paddock: str) -> str:
    """A galvanised trough, open at the top: its water is a prop of the scene."""
    tx, ty = trough_at(paddock)
    steel = pbr("0.70 0.72 0.75", roughness=0.35, metalness=0.85)
    parts = [
        visual("n", box(6, 0.08, 0.9), steel, "0 0.7 0.45 0 0 0"),
        visual("s", box(6, 0.08, 0.9), steel, "0 -0.7 0.45 0 0 0"),
        visual("e", box(0.08, 1.48, 0.9), steel, "2.96 0 0.45 0 0 0"),
        visual("w", box(0.08, 1.48, 0.9), steel, "-2.96 0 0.45 0 0 0"),
        visual("floor", box(6, 1.48, 0.1), steel, "0 0 0.05 0 0 0"),
        visual("float", box(0.5, 0.5, 0.3), steel, "3.3 0 0.75 0 0 0"),
        visual("pipe", cylinder(0.04, 8), pbr("0.15 0.15 0.15"), "7 0 0.04 0 1.5708 0"),
    ]
    return static(f"trough-{paddock}", f"{tx:.2f} {ty:.2f} 0 0 0 0", *parts)


def helipad() -> str:
    concrete = pbr("0.62 0.62 0.60", roughness=0.9)
    paint = pbr("0.95 0.95 0.92", roughness=0.6)
    yellow = pbr("0.95 0.75 0.10", roughness=0.6)
    parts = [
        visual("slab", box(10, 10, 0.12), concrete, "0 0 0.06 0 0 0"),
        visual("ring", cylinder(4.2, 0.02), yellow, "0 0 0.125 0 0 0", shadows=False),
        visual("ring-in", cylinder(3.9, 0.025), concrete, "0 0 0.13 0 0 0", shadows=False),
        visual("h-l", box(0.5, 3.2, 0.02), paint, "-1.0 0 0.145 0 0 0", shadows=False),
        visual("h-r", box(0.5, 3.2, 0.02), paint, "1.0 0 0.145 0 0 0", shadows=False),
        visual("h-bar", box(1.5, 0.5, 0.02), paint, "0 0 0.145 0 0 0", shadows=False),
    ]
    return static("helipad", "0 0 0 0 0 0", *parts)


def power_line() -> str:
    """Timber poles along the road, each with a crossarm, and three wires between them."""
    road_x, _ = xy("road")
    pole_x = road_x - 9
    timber = pbr("0.33 0.26 0.19", roughness=0.95)
    wire = pbr("0.15 0.15 0.15", roughness=0.5, metalness=0.6)
    parts: list[str] = []
    ys = list(range(-650, 951, 50))
    for k, y in enumerate(ys):
        parts.append(visual(f"pole{k}", cylinder(0.14, 10), timber, f"{pole_x} {y} 5 0 0 0"))
        parts.append(visual(f"arm{k}", box(2.4, 0.12, 0.12), timber, f"{pole_x} {y} 9.3 0 0 0"))
    length = ys[-1] - ys[0]
    for w, dx in enumerate((-1.0, 0.0, 1.0)):
        parts.append(
            visual(
                f"wire{w}",
                box(0.03, length, 0.03),
                wire,
                f"{pole_x + dx} {(ys[0] + ys[-1]) / 2} {9.45 if dx else 9.6} 0 0 0",
                shadows=False,
            )
        )
    return static("power-line", "0 0 0 0 0 0", *parts)


def hay_bales(rng: random.Random) -> list[str]:
    """Round bales of hay: a row by the shed and a few left in a paddock."""
    straw = pbr("0.80 0.66 0.36", roughness=1.0)
    end = pbr("0.70 0.55 0.28", roughness=1.0)
    spots = [(-48 + 2.2 * k, -10, 0.0) for k in range(6)]
    x, y = xy("north-flat")
    spots += [
        (x + 40 + rng.uniform(-12, 12), y - 50 + rng.uniform(-12, 12), rng.uniform(0, 3))
        for _ in range(7)
    ]
    out = []
    for k, (bx, by, yaw) in enumerate(spots):
        out.append(
            static(
                f"bale-{k}",
                f"{bx:.1f} {by:.1f} 0.8 0 0 {yaw:.2f}",
                visual("roll", cylinder(0.8, 1.2), straw, "0 0 0 1.5708 0 0"),
                visual("face", cylinder(0.78, 1.22), end, "0 0 0 1.5708 0 0"),
            )
        )
    return out


def windmill() -> str:
    """A Southern Cross windmill by the dam, pumping for the troughs: a lattice tower,
    a wheel of blades and a tail vane."""
    dx, dy = xy("dam")
    steel = pbr("0.55 0.56 0.58", roughness=0.45, metalness=0.8)
    blade = pbr("0.80 0.80 0.78", roughness=0.4, metalness=0.7)
    red = pbr("0.70 0.15 0.10", roughness=0.5, metalness=0.3)
    height = 11.0
    parts: list[str] = []
    for k, (lx, ly) in enumerate(((1.4, 1.4), (1.4, -1.4), (-1.4, 1.4), (-1.4, -1.4))):
        # Each leg leans in to meet the others under the head.
        tilt = math.atan2(math.hypot(lx, ly) - 0.25, height)
        yaw = math.atan2(ly, lx)
        parts.append(
            visual(
                f"leg{k}",
                cylinder(0.05, height / math.cos(tilt)),
                steel,
                f"{lx / 2:.3f} {ly / 2:.3f} {height / 2} 0 {-tilt:.4f} {yaw:.4f}",
            )
        )
    for k, z in enumerate((3, 6, 9)):
        span = 2.8 * (1 - z / height) + 0.5
        parts.append(visual(f"ring{k}", box(span, span, 0.04), steel, f"0 0 {z} 0 0 0"))
    parts.append(visual("head", box(0.9, 0.4, 0.4), red, f"0 0 {height + 0.2} 0 0 0"))
    for k in range(18):
        a = 2 * math.pi * k / 18
        parts.append(
            visual(
                f"blade{k}",
                box(0.04, 0.35, 1.6),
                blade,
                f"0.55 {1.4 * math.sin(a):.3f} {height + 0.4 + 1.4 * math.cos(a):.3f} {a:.4f} 0 0",
            )
        )
    parts.append(visual("hub", cylinder(0.25, 0.3), red, f"0.55 0 {height + 0.4} 0 1.5708 0"))
    parts.append(visual("boom", box(2.2, 0.06, 0.06), steel, f"-1.1 0 {height + 0.35} 0 0 0"))
    parts.append(visual("vane", box(0.04, 1.6, 1.0), red, f"-2.2 0 {height + 0.4} 0 0 1.5708"))
    return static("windmill", f"{dx - 8:.1f} {dy + 6:.1f} 0 0 0 0.6", *parts)


def rain_tanks() -> list[str]:
    """Corrugated rainwater tanks by the homestead."""
    tank = pbr("0.80 0.82 0.84", texture="CorrugatedSteel005", roughness=0.45, metalness=0.2)
    lid = pbr("0.55 0.56 0.58", roughness=0.5, metalness=0.4)
    return [
        static(
            f"rain-tank-{k}",
            f"{x} {y} 0 0 0 0",
            visual("shell", cylinder(1.8, 2.6), tank, "0 0 1.3 0 0 0"),
            visual("lid", cylinder(1.85, 0.08), lid, "0 0 2.64 0 0 0"),
        )
        for k, (x, y) in enumerate(((62, 10), (66.5, 10)))
    ]


def outback_trees(rng: random.Random) -> list[str]:
    """Lone trees across the open country, thinning out with distance."""
    out = []
    for k in range(70):
        r = rng.uniform(450, 1500)
        a = rng.uniform(0, 2 * math.pi)
        x, y = 150 + r * math.cos(a), 150 + r * math.sin(a)
        kind = "model://Oak tree" if rng.random() < 0.8 else "model://Pine Tree"
        out.append(
            include(f"lone-tree-{k}", kind, f"{x:.0f} {y:.0f} 0 0 0 {rng.uniform(0, 6.28):.2f}")
        )
    return out


def hills(rng: random.Random) -> list[str]:
    """Low ridges on the horizon, so the plain has an edge."""
    land = pbr("0.70 0.58 0.42", texture="Ground037", roughness=1.0)
    out = []
    for k in range(16):
        a = 2 * math.pi * k / 16 + rng.uniform(-0.1, 0.1)
        r = rng.uniform(1550, 1850)
        out.append(
            static(
                f"hill-{k}",
                f"{150 + r * math.cos(a):.0f} {150 + r * math.sin(a):.0f} -20 0 0 {a + 1.5708:.3f}",
                visual(
                    "v",
                    ellipsoid(rng.uniform(550, 850), rng.uniform(160, 260), rng.uniform(70, 130)),
                    land,
                ),
            )
        )
    return out


def neighbour() -> list[str]:
    """The neighbour's paddocks, east of the boundary: fenced pasture, no stock."""
    nx, _ = xy("neighbour")
    out = []
    for k, (dx, y) in enumerate(((90, 90), (90, 240), (240, 90), (240, 240))):
        tint = ("0.85 0.95 0.70", "0.95 0.90 0.65", "0.90 0.92 0.68", "0.98 0.88 0.62")[k]
        x = nx - 50 + dx
        out.append(
            static(
                f"neighbour-pasture-{k}",
                f"{x} {y} 0.03 0 0 0",
                visual(
                    "v", mesh(f"{GROUND}/paddock.obj"), pbr(tint, texture="Grass004"), shadows=False
                ),
            )
        )
    return out


def trees(rng: random.Random) -> list[str]:
    """A pine windbreak to the south and west, gums (oaks here) along the creek and
    round the homestead and the dam, and a shade tree or two in each paddock."""
    out: list[str] = []
    n = 0

    def tree(kind: str, x: float, y: float) -> None:
        nonlocal n
        n += 1
        uri = "model://Pine Tree" if kind == "pine" else "model://Oak tree"
        out.append(include(f"tree-{n}", uri, f"{x:.1f} {y:.1f} 0 0 0 {rng.uniform(0, 6.28):.2f}"))

    west = xy("north-flat")[0] - PADDOCK_M / 2 - 10
    for k in range(34):
        tree("pine", west + rng.uniform(-1.5, 1.5), -30 + k * 10 + rng.uniform(-2, 2))
    for k in range(40):
        tree("pine", -110 + k * 10 + rng.uniform(-2, 2), -48 + rng.uniform(-1.5, 1.5))
    for y in range(-20, 330, 26):
        tree("oak", creek_x(y) + rng.choice((-1, 1)) * rng.uniform(9, 16), y + rng.uniform(-6, 6))
    for x, y in ((28, -22), (62, -20), (66, 14), (24, 16), (80, -6)):
        tree("oak", x + rng.uniform(-3, 3), y + rng.uniform(-3, 3))
    dx, dy = xy("dam")
    for a in range(0, 360, 50):
        r = math.radians(a + rng.uniform(-10, 10))
        tree("oak", dx + 25 + 40 * math.cos(r), dy - 20 + 40 * math.sin(r))
    for paddock in PADDOCKS:
        x, y = xy(paddock)
        tree("oak", x + 45 + rng.uniform(-6, 6), y + 45 + rng.uniform(-6, 6))
        if rng.random() < 0.6:
            tree("oak", x - 50 + rng.uniform(-5, 5), y - 40 + rng.uniform(-5, 5))
    return out


def scenery() -> list[str]:
    rng = random.Random(SEED)
    dry = pbr("0.95 0.88 0.70", texture="Ground037")
    gravel = pbr("1 1 1", texture="Ground081")
    dirt = pbr("0.95 0.86 0.74", texture="Ground103")
    water = pbr("0.10 0.24 0.30", roughness=0.04, metalness=0.2)
    parts = [
        static(
            "ground",
            "0 0 0 0 0 0",
            visual("v", mesh(f"{GROUND}/outback.obj"), dry, shadows=False),
            collide='<collision name="c"><geometry><plane><normal>0 0 1</normal><size>4000 4000</size></plane></geometry></collision>',
        )
    ]
    # The paddocks: each its own shade of pasture, some greener and some drier.
    tints = [
        "0.80 1.0 0.70",
        "1.0 0.90 0.62",
        "0.86 1.0 0.78",
        "1.0 0.84 0.58",
        "0.76 0.95 0.66",
        "0.96 0.94 0.72",
    ]
    for paddock, tint in zip(PADDOCKS, tints, strict=True):
        x, y = xy(paddock)
        parts.append(
            static(
                f"pasture-{paddock}",
                f"{x} {y} 0.03 0 0 0",
                visual(
                    "v", mesh(f"{GROUND}/paddock.obj"), pbr(tint, texture="Grass004"), shadows=False
                ),
            )
        )
        parts.append(fence(paddock))
        parts.append(trough(paddock))
    road_x, _ = xy("road")
    parts.append(
        static(
            "road",
            f"{road_x} 150 0.05 0 0 0",
            visual("v", mesh(f"{GROUND}/road.obj"), gravel, shadows=False),
        )
    )
    # The farm track: in from the road to the yard, then up the lanes between paddocks.
    parts.append(
        static(
            "track-in",
            f"{(road_x + 20) / 2} 0 0.04 0 0 0",
            visual("v", mesh(f"{GROUND}/track_in.obj"), gravel, shadows=False),
        )
    )
    parts.append(
        static(
            "track-lane",
            f"{UNIT_M / 2} 165 0.04 0 0 0",
            visual("v", mesh(f"{GROUND}/track_lane.obj"), dirt, shadows=False),
        )
    )
    parts.append(
        static(
            "track-cross",
            f"{UNIT_M} 165 0.045 0 0 0",
            visual("v", mesh(f"{GROUND}/track_cross.obj"), dirt, shadows=False),
        )
    )
    parts.append(
        static(
            "yard", "5 -2 0.035 0 0 0", visual("v", mesh(f"{GROUND}/yard.obj"), dirt, shadows=False)
        )
    )
    # The creek and the dam.
    parts.append(
        static(
            "creek-banks",
            "0 0 0.05 0 0 0",
            visual("v", mesh(f"{GROUND}/creek_banks.obj"), dirt, shadows=False),
        )
    )
    parts.append(
        static(
            "creek-water",
            "0 0 0.07 0 0 0",
            visual("v", mesh(f"{GROUND}/creek_water.obj"), water, shadows=False),
        )
    )
    dx, dy = xy("dam")
    parts.append(
        static(
            "dam-bank",
            f"{dx + 25} {dy - 20} 0.05 0 0 0",
            visual("v", mesh(f"{GROUND}/dam_bank.obj"), dirt, shadows=False),
        )
    )
    parts.append(
        static(
            "dam-water",
            f"{dx + 25} {dy - 20} 0.08 0 0 0",
            visual("v", mesh(f"{GROUND}/dam_water.obj"), water, shadows=False),
        )
    )
    # The yard: the pad the drone flies from, the shed beside it, the homestead and
    # its water tower.
    parts.append(helipad())
    corrugated = pbr("0.82 0.84 0.86", texture="CorrugatedSteel005", roughness=0.55, metalness=0.0)
    parts.append(
        static(
            "shed",
            "-30 2 0 0 0 0",
            visual("v", mesh("model://farm_shed/meshes/shed.obj"), corrugated),
            visual(
                "door", box(0.1, 5, 3.6), pbr("0.15 0.15 0.16", roughness=0.8), "9.02 0 1.8 0 0 0"
            ),
            collide=f'<collision name="c"><pose>0 0 2.5 0 0 0</pose><geometry>{box(18, 10, 5)}</geometry></collision>',
        )
    )
    # Fuel's house and water tower carry Ogre 1 material scripts, which Gazebo's Ogre 2
    # draws black: their meshes are drawn here with their own textures instead.
    textures = "model://house_1/materials/textures"
    house = (
        "<material><diffuse>1 1 1 1</diffuse><pbr><metal>"
        f"<albedo_map>{textures}/House_1_Diffuse.png</albedo_map>"
        f"<normal_map>{textures}/House_1_Normal.png</normal_map>"
        "<roughness>0.8</roughness></metal></pbr></material>"
    )
    parts.append(
        static(
            "homestead",
            "48 2 0 0 0 3.1416",
            visual(
                "v",
                "<mesh><uri>model://house_1/meshes/house_1.dae</uri><scale>1.5 1.5 1.5</scale></mesh>",
                house,
            ),
        )
    )
    tower = (
        "<material><diffuse>1 1 1 1</diffuse><pbr><metal>"
        "<albedo_map>model://water_tower/materials/textures/water_tower_diffuse.jpg</albedo_map>"
        "<roughness>0.6</roughness><metalness>0.5</metalness></metal></pbr></material>"
    )
    parts.append(
        static(
            "tower",
            "-32 -20 0 0 0 0",
            visual("v", mesh("model://water_tower/meshes/water_tower.dae"), tower),
        )
    )
    parts += trees(rng)
    parts.append(power_line())
    parts += hay_bales(rng)
    parts.append(windmill())
    parts += rain_tanks()
    parts += outback_trees(rng)
    parts += hills(rng)
    parts += neighbour()
    return parts


def world() -> str:
    # Mid-morning in the Riverina: the sun low in the north-east, long shadows.
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by scripts/gz_world.py from the demo's map; do not edit. -->
<sdf version="1.9">
  <world name="farm">
    <physics type="ode">
      <max_step_size>0.004</max_step_size>
      <real_time_factor>1.0</real_time_factor>
      <real_time_update_rate>250</real_time_update_rate>
    </physics>
    <gravity>0 0 -9.8</gravity>
    <magnetic_field>6e-06 2.3e-05 -4.2e-05</magnetic_field>
    <atmosphere type="adiabatic"/>
    <scene>
      <grid>false</grid>
      <ambient>0.42 0.42 0.45 1</ambient>
      <background>0.55 0.72 0.92 1</background>
      <shadows>true</shadows>
      <sky></sky>
    </scene>
    <light name="sun" type="directional">
      <pose>0 0 500 0 0 0</pose>
      <cast_shadows>true</cast_shadows>
      <intensity>1.4</intensity>
      <direction>-0.45 -0.55 -0.70</direction>
      <diffuse>1.0 0.95 0.86 1</diffuse>
      <specular>0.6 0.58 0.52 1</specular>
    </light>
    <light name="sky-fill" type="directional">
      <cast_shadows>false</cast_shadows>
      <intensity>0.35</intensity>
      <direction>0.4 0.5 -0.75</direction>
      <diffuse>0.55 0.65 0.85 1</diffuse>
      <specular>0 0 0 1</specular>
    </light>
    {"".join(scenery())}
    <spherical_coordinates>
      <surface_model>EARTH_WGS84</surface_model>
      <world_frame_orientation>ENU</world_frame_orientation>
      <latitude_deg>{ORIGIN.lat}</latitude_deg>
      <longitude_deg>{ORIGIN.lon}</longitude_deg>
      <elevation>{ORIGIN.alt_m}</elevation>
    </spherical_coordinates>
  </world>
</sdf>
"""


def ground_meshes() -> dict[str, str]:
    road_x, _ = xy("road")
    creek = creek_points()
    return {
        "outback.obj": quad_obj(4000, 4000, 30),
        "paddock.obj": quad_obj(PADDOCK_M, PADDOCK_M, 5),
        "road.obj": quad_obj(9, 1600, 6),
        "track_in.obj": quad_obj(abs(road_x) - 20, 5, 5),
        "track_lane.obj": quad_obj(5, 330, 5),
        "track_cross.obj": quad_obj(2.5 * UNIT_M + 40, 5, 5),
        "yard.obj": quad_obj(110, 40, 5),
        "creek_banks.obj": strip_obj(creek, 16, 5),
        "creek_water.obj": strip_obj(creek, 7, 5),
        "dam_bank.obj": disc_obj(36, 5),
        "dam_water.obj": disc_obj(28, 5),
    }


def write(out: Path) -> None:
    models = out / "farm_models"
    files: dict[Path, str] = {out / "farm.sdf": world()}
    for name, text in ground_meshes().items():
        files[models / "farm_ground" / "meshes" / name] = text
    files[models / "farm_shed" / "meshes" / "shed.obj"] = shed_obj(18, 10, 4.2, 6.2, 2.0)
    for name, sdf in (
        *(
            (f"farm_hereford_{i}", cattle_sdf(f"farm_hereford_{i}", 1.0, red, grazing))
            for i, (red, grazing) in enumerate(COW_VARIANTS)
        ),
        ("farm_calf", cattle_sdf("farm_calf", 0.55, HEREFORD_REDS[1], False)),
        ("farm_motorbike", motorbike_sdf()),
        ("farm_quad_bike", quad_bike_sdf()),
        ("farm_toolbox", toolbox_sdf()),
        ("farm_smoke", smoke_sdf()),
    ):
        files[models / name / "model.sdf"] = sdf
        files[models / name / "model.config"] = model_config(name)
    for name in ("farm_ground", "farm_shed"):
        files[models / name / "model.config"] = model_config(name)
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    print(f"wrote {out / 'farm.sdf'} and {len(files) - 1} files in {models}")


if __name__ == "__main__":
    write(Path(sys.argv[1] if len(sys.argv) > 1 else "sim"))
