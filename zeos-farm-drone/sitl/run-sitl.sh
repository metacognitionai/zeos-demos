#!/usr/bin/env bash
# PX4 v1.17.0 SITL with Gazebo Harmonic over the farm, in a tmux session `zeos:px4`.
#
# The checkouts sit side by side in one directory, which the container sees as /zeos:
#   zeos-demos/  sim/PX4-Autopilot/  sim/home/
# sim/home is the container's home: Gazebo's GUI settings, ccache, and the demo's venv.
#
# Runs as the calling user so the files it writes stay theirs. These settings are
# load-bearing:
#   --tmpfs /tmp/.X11-unix  Xvfb, the image's virtual display, cannot create its socket
#                           directory as a non-root user otherwise, and Gazebo then
#                           never reports its world ready.
#   GZ_PARTITION=zeos       gz-transport's default partition includes the user name,
#                           which the host's uid lacks in the container: without an
#                           explicit partition PX4 never discovers the Gazebo world.
#   PX4_GZ_WORLD=farm       the farm world, written by the demo's scripts/gz_world.py and
#                           copied into PX4's worlds directory, where PX4 looks for it.
#   --device nvidia.com/gpu=$ZEOS_GPU  the GPU (0 unless ZEOS_GPU says otherwise), with
#                           the graphics driver: inside.sh starts Gazebo's server on it,
#                           and view.sh its 3D window through VirtualGL.
#   --group-add video,render  the groups that own /dev/dri on the host.
#   PX4_HOME_*              the world origin: the farm's shed, on the Riverine plain
#                           near Hay, NSW (zeos_farm_drone/geo.py has the same point).
# The demo runs in this container too (docker exec), so MAVLink stays on its loopback.
# Its page listens on 18767 and noVNC on 6080, published as 18767 and 18768 on the
# host's loopback only.
set -euo pipefail
DEMO=$(cd "$(dirname "$0")/.." && pwd)
ROOT=$(cd "$DEMO/../.." && pwd)
S=$ROOT/sim
MODEL=${1:-gz_x500_gimbal}
UV=${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}
WHO=$(id -un)
mkdir -p "$S/home/.gz/sim/8"
cp "$DEMO/sitl/gui.config" "$S/home/.gz/sim/8/gui.config"
cp "$DEMO/sim/farm.sdf" "$S/PX4-Autopilot/Tools/simulation/gz/worlds/farm.sdf"
docker rm -f zeos-px4 >/dev/null 2>&1 || true
tmux -L zeos kill-session -t px4 2>/dev/null || true
tmux -L zeos new-session -d -s px4 "docker run --rm -it --name zeos-px4 \
  --user $(id -u):$(id -g) --tmpfs /tmp/.X11-unix:mode=1777 \
  --device nvidia.com/gpu=${ZEOS_GPU:-0} -e NVIDIA_DRIVER_CAPABILITIES=all \
  --group-add $(getent group video | cut -d: -f3) --group-add $(getent group render | cut -d: -f3) \
  -e HOME=/home/$WHO -e GZ_PARTITION=zeos \
  -e PX4_HOME_LAT=-34.5 -e PX4_HOME_LON=144.85 -e PX4_HOME_ALT=90 \
  -p 127.0.0.1:18767:18767 -p 127.0.0.1:18768:6080 \
  -v $S/home:/home/$WHO -v $S/PX4-Autopilot:/px4 -w /px4 \
  -v $ROOT:/zeos -v $UV:/usr/local/bin/uv:ro \
  zeos/px4-gz:1.17.0 bash /zeos/zeos-demos/zeos-farm-drone/sitl/inside.sh $MODEL"
echo "PX4 SITL ($MODEL) over the farm starting in tmux session zeos:px4; container zeos-px4"
