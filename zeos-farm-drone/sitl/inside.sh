#!/usr/bin/env bash
# The command of the zeos-px4 container: Gazebo's server over the farm on the GPU,
# then PX4 SITL joining it.
#
# The server is started here rather than by PX4 so that it renders on the GPU: with no
# display and --headless-rendering, Ogre takes NVIDIA's EGL device (PX4 would start it
# on the virtual display, which renders in software). PX4_GZ_STANDALONE tells PX4 the
# world is already running. view.sh starts the 3D window separately.
set -euo pipefail
MODEL=${1:?"usage: inside.sh PX4_MODEL (e.g. gz_x500_gimbal)"}
DEMO=$(cd "$(dirname "$0")/.." && pwd)
# PX4's build writes gz_env.sh: the first run builds SITL, later runs find nothing to do.
make -C /px4 px4_sitl
# gz_env.sh appends to these, so they must exist under set -u.
export GZ_SIM_RESOURCE_PATH=${GZ_SIM_RESOURCE_PATH:-} GZ_SIM_SYSTEM_PLUGIN_PATH=${GZ_SIM_SYSTEM_PLUGIN_PATH:-}
source /px4/build/px4_sitl_default/rootfs/gz_env.sh
export GZ_SIM_RESOURCE_PATH=$GZ_SIM_RESOURCE_PATH:$DEMO/sim/models:$DEMO/sim/farm_models
export GZ_SIM_SERVER_CONFIG_PATH=$DEMO/sim/server.config
DISPLAY= gz sim -s -r --headless-rendering "$PX4_GZ_WORLDS/farm.sdf" &
until gz topic -l | grep -q "^/world/farm/clock"; do sleep 1; done
cd /px4
PX4_GZ_STANDALONE=1 PX4_GZ_WORLD=farm exec make px4_sitl "$MODEL"
