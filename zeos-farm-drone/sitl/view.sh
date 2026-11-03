#!/usr/bin/env bash
# Gazebo's 3D window on the PX4 container's virtual display, rendered on the GPU through
# VirtualGL, and shared with a browser: x11vnc on the container's loopback, noVNC on
# 6080 (the host's 18768). Then Gazebo's camera follows the drone, looking at it.
set -euo pipefail
docker exec -d zeos-px4 bash -c 'source /px4/build/px4_sitl_default/rootfs/gz_env.sh &&
  export GZ_SIM_RESOURCE_PATH=$GZ_SIM_RESOURCE_PATH:/zeos/zeos-demos/zeos-farm-drone/sim/models:/zeos/zeos-demos/zeos-farm-drone/sim/farm_models &&
  exec vglrun -d egl gz sim -g > /tmp/gui.log 2>&1'
docker exec -d zeos-px4 x11vnc -display :99 -localhost -forever -shared -nopw -quiet -rfbport 5900
docker exec -d zeos-px4 websockify --web /usr/share/novnc 6080 localhost:5900
# The window ignores a target it has not loaded yet, and loading the farm takes it a
# while: ask until it says, on /gui/currently_tracked, that it follows the drone.
until docker exec zeos-px4 gz topic -l | grep -q "^/gui/track"; do sleep 1; done
until docker exec zeos-px4 timeout 3 gz topic -e -t /gui/currently_tracked -n 1 2>/dev/null | grep -q x500_gimbal_0; do
  docker exec zeos-px4 gz topic -t /gui/track -m gz.msgs.CameraTrack \
    -p 'track_mode: FOLLOW_LOOK_AT, follow_target: {name: "x500_gimbal_0"}, track_target: {name: "x500_gimbal_0"},
        follow_offset: {x: -10, y: -10, z: 5}, follow_pgain: 0.5, track_pgain: 0.5'
done
echo "noVNC on the host's 127.0.0.1:18768/vnc.html"
