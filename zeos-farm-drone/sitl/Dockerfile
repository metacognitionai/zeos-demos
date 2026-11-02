# PX4 SITL with Gazebo Harmonic, for the zeos-farm-drone adapter.
#
# PX4 v1.17.0 Tools/setup/Dockerfile, with the simulator kept (its own image passes
# --no-sim-tools) and the NuttX cross-compiler left out (SITL builds for the host).
# The PX4 source and its build live on the host and are mounted at /px4. Built with
# PX4's own setup directory as the context, which holds the files copied below:
#   docker build -t zeos/px4-gz:1.17.0 -f sitl/Dockerfile ../../sim/PX4-Autopilot/Tools/setup
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV DISPLAY=:99
ENV TERM=xterm
ENV TZ=UTC
ENV RUNS_IN_DOCKER=true

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY requirements.txt /tmp/requirements.txt
COPY ubuntu.sh /tmp/ubuntu.sh

RUN apt-get update && apt-get install -y --no-install-recommends sudo lsb-release wget ca-certificates xvfb \
    && bash /tmp/ubuntu.sh --no-nuttx \
    && python3 -m pip install --break-system-packages mavsdk==4.0.6 \
    && rm -rf /var/lib/apt/lists/* /root/.cache

RUN git config --global --add safe.directory "*"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["/bin/bash"]

# Watching the simulation: Gazebo's 3D window on the virtual display, shared over VNC
# and served to a browser by noVNC (loopback only; reached through an SSH tunnel).
RUN apt-get update && apt-get install -y --no-install-recommends x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*

# Gazebo's 3D window on the GPU: VirtualGL hands the window's OpenGL to the NVIDIA
# driver over EGL (no X server on the GPU needed) and copies each frame back to the
# virtual display. Run the window under `vglrun -d egl`.
RUN wget -q -O /tmp/virtualgl.deb \
      https://github.com/VirtualGL/virtualgl/releases/download/3.1.5/virtualgl_3.1.5_amd64.deb \
    && apt-get update && apt-get install -y --no-install-recommends /tmp/virtualgl.deb mesa-utils \
    && rm -rf /var/lib/apt/lists/* /tmp/virtualgl.deb
