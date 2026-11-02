#!/usr/bin/env bash
# The whole farm under PX4 in Gazebo, from nothing: SITL over the farm world, Gazebo's
# view in a browser, and the farm page driving the drone and showing the farm's events
# in Gazebo. Reach it from another machine through a tunnel:
#   ssh -N -L 18767:127.0.0.1:18767 -L 18768:127.0.0.1:18768 HOST
# then the page on localhost:18767 and Gazebo on localhost:18768/vnc.html.
# Any arguments go to the page's server, e.g. --machine claude.
set -euo pipefail
DEMO=$(cd "$(dirname "$0")/.." && pwd)
S=$(cd "$DEMO/../.." && pwd)/sim
"$DEMO/sitl/run-sitl.sh"
until docker exec zeos-px4 gz service -l 2>/dev/null | grep -q /world/farm/create_multiple; do sleep 2; done
until grep -q "home set" <(tmux -L zeos capture-pane -pt px4 -S -200); do sleep 2; done
"$DEMO/sitl/view.sh"
# The wait below reads this log, so a previous run's must not answer it.
rm -f "$S/farm-px4.log"
docker exec -d -w /zeos/zeos-demos/zeos-farm-drone -e UV_PROJECT_ENVIRONMENT="/home/$(id -un)/venv-farm" \
  zeos-px4 sh -c "uv run --extra px4 --extra claude zeos-farm-drone serve --device px4 --gazebo farm \
  --camera x500_gimbal_0 --host 0.0.0.0 --port 18767 --no-open $* > /zeos/sim/farm-px4.log 2>&1"
until grep -q "PX4 connected" "$S/farm-px4.log" 2>/dev/null; do sleep 2; done
echo "farm page on 127.0.0.1:18767, Gazebo on 127.0.0.1:18768/vnc.html (log: $S/farm-px4.log)"
