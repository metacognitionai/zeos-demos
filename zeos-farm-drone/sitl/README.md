# The farm under PX4 SITL in Gazebo: setting it up from nothing

This is the whole setup, step by step, as it was run from an empty directory on the
machine the demo was built on. Gazebo is not installed on the host: it is in the Docker
image, with PX4's toolchain, and the host needs only a GPU, Docker and a few tools.

What it gives you: PX4 v1.17.0 SITL flying an x500 with a gimbal camera over the farm in
Gazebo Harmonic, the GPU rendering both the camera and Gazebo's 3D window, the window in
a browser through noVNC, and the farm's page driving the drone.

## What the host needs

| | what | as tested |
| --- | --- | --- |
| OS | Linux, x86_64 | Ubuntu 22.04.5 |
| GPU | NVIDIA, with its driver | two RTX A6000s, driver 580.178.04 |
| Docker | Docker Engine, your user in the `docker` group | 29.2.0 |
| GPUs in containers | NVIDIA Container Toolkit, with a CDI specification | 1.18.2 |
| | tmux | 3.2a |
| | uv | 0.12.19 |
| | git, and about 10 GB free: 2.8 GB for PX4, 3.5 GB for the image | |

The farm page and its tests run anywhere uv does (see the demo's README); only this setup
needs Linux and an NVIDIA GPU.

## 1. Check the host

```bash
nvidia-smi                      # the GPUs and the driver
docker run --rm hello-world     # Docker, without sudo
nvidia-ctk cdi list             # must list nvidia.com/gpu=0 (and =1 and so on)
getent group video render       # the groups that own /dev/dri; run-sitl.sh adds both
tmux -V && uv --version
```

The container asks for its GPU as a CDI device, `nvidia.com/gpu=N`. If `nvidia-ctk cdi
list` lists none, write the specification (NVIDIA's own command; it needs root):

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
```

A specification under `/var/run/cdi` is gone after a reboot unless the toolkit's
`nvidia-cdi-refresh` service writes it again; one under `/etc/cdi` stays.

## 2. Lay out the checkouts

Everything lives under one directory, called `ROOT` here, which the container sees as
`/zeos`:

```text
ROOT/
  zeos-demos/                 this repository
  sim/PX4-Autopilot/          PX4 v1.17.0, with its submodules
  sim/home/                   the container's home: Gazebo's GUI settings, ccache, the demo's venv
  sim/farm-px4.log            the farm page's log
```

```bash
mkdir ROOT && cd ROOT
git clone https://github.com/metacognitionai/zeos-demos.git
mkdir sim
```

The kernel, `zeos`, is not checked out here: the demo's `uv.lock` pins it as a git
dependency, and the container installs it.

## 3. Clone PX4

```bash
git clone --recursive --branch v1.17.0 https://github.com/PX4/PX4-Autopilot.git sim/PX4-Autopilot
```

That is 2.8 GB with its 39 submodules, and took 4 minutes 50 seconds here. PX4 itself
is not changed: `run-sitl.sh` copies the farm's world into its worlds directory.

## 4. Build the image

```bash
docker build -t zeos/px4-gz:1.17.0 -f zeos-demos/zeos-farm-drone/sitl/Dockerfile sim/PX4-Autopilot/Tools/setup
```

The build context is PX4's own `Tools/setup`, whose `ubuntu.sh` installs the toolchain
and Gazebo. The image (3.3 GB) is Ubuntu 24.04 with:

| | version |
| --- | --- |
| PX4's build toolchain, without NuttX's cross-compiler (SITL builds for the host) | PX4 v1.17.0's |
| Gazebo Harmonic (`gz-harmonic`, `gz sim` 8.15.0) | from packages.osrfoundation.org |
| MAVSDK for Python, for the system Python | 4.0.6 |
| Xvfb, x11vnc, noVNC and websockify | Ubuntu's |
| VirtualGL | 3.1.5 |

From nothing, with no build cache, it took 6 minutes 30 seconds here.

## 5. Download the models and textures

```bash
(cd zeos-demos/zeos-farm-drone && uv run scripts/fetch_assets.py sim/models)
```

Eight Gazebo Fuel models (the vehicles, the house, the water tower, the trees, the smoke)
and five ambientCG textures, each at a pinned version, into `sim/models`, with an
`ATTRIBUTION.md` of where each came from and its licence (CC0 1.0 or CC BY 4.0). They are
not in the repository; `sim/models.sha256` is, and the script checks every file against
it, so what you have is byte for byte what the farm was built and filmed with. It ends:

```text
checked  103 files against models.sha256: all as recorded
```

Everything else Gazebo shows -- the ground, the creek and the dam, the shed, the herds,
the calves, the motorbike, the quad bike, the toolbox, the smoke column -- is the demo's
own, committed in `sim/farm.sdf` and `sim/farm_models/`, written by
`scripts/gz_world.py`.

## 6. Start it

```bash
ZEOS_GPU=0 zeos-demos/zeos-farm-drone/sitl/start.sh
```

`ZEOS_GPU` is the GPU to use, by the index `nvidia-smi` gives it. `start.sh` starts the
container `zeos-px4` in a tmux session, starts Gazebo's server over the farm on the GPU
and PX4 SITL in it, Gazebo's window behind noVNC, and then the farm page, and returns when
PX4 is connected:

```text
PX4 SITL (gz_x500_gimbal) over the farm starting in tmux session zeos:px4; container zeos-px4
noVNC on the host's 127.0.0.1:18768/vnc.html
farm page on 127.0.0.1:18767, Gazebo on 127.0.0.1:18768/vnc.html (log: ROOT/sim/farm-px4.log)
```

The first start also builds PX4 SITL (1141 targets, with an empty ccache; 20 seconds on
32 cores here) and installs the page's Python environment, the kernel among it, into
`sim/home`: the whole of it took 49 seconds here. A later start finds the build done.

## 7. Watch it

Both pages listen on the host's loopback only. From your own machine:

```bash
ssh -N -L 18767:127.0.0.1:18767 -L 18768:127.0.0.1:18768 HOST
```

then open the farm page at `http://localhost:18767` and Gazebo at
`http://localhost:18768/vnc.html?autoconnect=1&resize=scale`. Click **New day**: the drone
takes off from its pad beside the shed, and Gazebo's camera follows it.

## 8. With Claude choosing the commands

```bash
cd zeos-demos/zeos-farm-drone && cp .env.example .env && chmod 600 .env
```

Put your key after `ANTHROPIC_API_KEY=`, then:

```bash
ZEOS_GPU=0 zeos-demos/zeos-farm-drone/sitl/start.sh --machine claude
```

Any arguments to `start.sh` go to the page's server.

## Running it day to day

| | |
| --- | --- |
| start, or restart | `ZEOS_GPU=0 zeos-demos/zeos-farm-drone/sitl/start.sh` |
| stop | `docker rm -f zeos-px4` |
| PX4's console | `tmux -L zeos attach -t px4` (detach with Ctrl-B D) |
| the page's log | `ROOT/sim/farm-px4.log` |
| Gazebo's window's log | `docker exec zeos-px4 cat /tmp/gui.log` |
| what Gazebo publishes | `docker exec zeos-px4 gz topic -l` |
| each session's recording | `zeos-demos/zeos-farm-drone/runs/` |

## When it does not start

- **`docker run` refuses the `nvidia.com/gpu` device**: there is no CDI specification,
  or none for that GPU. See step 1; after a reboot, check `nvidia-ctk cdi list` first.
- **Gazebo never reports the world ready, and `start.sh` waits forever**: look at PX4's
  console. The container needs `--tmpfs /tmp/.X11-unix` for Xvfb and `GZ_PARTITION=zeos`
  for PX4 to find the world; `run-sitl.sh` sets both, and says why.
- **Gazebo's window is black or very slow**: it is rendering in software. It must run
  under `vglrun -d egl`, as `view.sh` starts it, with `NVIDIA_DRIVER_CAPABILITIES=all`.
- **`bind: Address already in use` on 18767 or 18768**: on the host, another container or
  service holds the port; on your machine, another tunnel does, and that one already
  works.
- **Another user on a shared host**: the container name `zeos-px4`, the tmux socket
  `zeos` and the ports are fixed, so one setup runs per host at a time.
