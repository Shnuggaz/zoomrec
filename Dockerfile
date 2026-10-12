FROM ubuntu:24.04

# Zoom client to install. "latest" downloads the current release from zoom.us,
# any other value is used as a version, e.g. --build-arg ZOOM_VERSION=6.4.6.1370
# ZOOM_DEB_URL overrides the download URL completely.
ARG ZOOM_VERSION=latest
ARG ZOOM_DEB_URL=""

ENV HOME=/home/zoomrec \
    TZ=Europe/Vienna \
    TERM=xfce4-terminal \
    START_DIR=/start \
    DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    VNC_RESOLUTION=1920x1080 \
    VNC_COL_DEPTH=24 \
    VNC_PW=zoomrec \
    VNC_PORT=5901 \
    DISPLAY=:1 \
    XDG_SESSION_TYPE=x11 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    # Chromium sandbox of Qt WebEngine (used by Zoom) does not work in unprivileged containers
    QTWEBENGINE_DISABLE_SANDBOX=1

# Ubuntu 24.04 ships a default "ubuntu" user with uid 1000.
# Remove it so zoomrec gets uid 1000, which matches most host users for volume permissions.
RUN userdel -r ubuntu 2>/dev/null || true && \
    useradd -ms /bin/bash -u 1000 zoomrec -d ${HOME}
WORKDIR ${HOME}

# Base system, VNC, Xfce, PulseAudio, FFmpeg, Python
RUN apt-get update && \
    apt-get install --no-install-recommends -y \
        ca-certificates \
        wget \
        locales \
        tzdata \
        procps \
        xdotool \
        wmctrl \
        tigervnc-standalone-server \
        tigervnc-tools \
        xfce4 \
        xfce4-terminal \
        xfce4-pulseaudio-plugin \
        xfce4-notifyd \
        xfce4-power-manager-plugins \
        xubuntu-icon-theme \
        pulseaudio \
        pulseaudio-utils \
        pavucontrol \
        ibus \
        dbus-user-session \
        dbus-x11 \
        dbus \
        at-spi2-core \
        xauth \
        x11-xserver-utils \
        x11-utils \
        libxkbcommon-x11-0 \
        ffmpeg \
        python3 \
        python3-venv \
        python3-tk \
        scrot && \
    locale-gen en_US.UTF-8 && \
    ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime && echo ${TZ} > /etc/timezone && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Python dependencies in a venv (Ubuntu 24.04 forbids pip into the system Python, PEP 668)
COPY res/requirements.txt ${HOME}/res/requirements.txt
RUN python3 -m venv ${VIRTUAL_ENV} && \
    pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r ${HOME}/res/requirements.txt

# Runtime libraries Zoom loads but does not declare in its .deb
RUN apt-get update && \
    apt-get install --no-install-recommends -y \
        libegl1 \
        libgl1 \
        libgles2 \
        libopengl0 \
        libglx0 \
        libgbm1 \
        libdrm2 \
        libnss3 \
        libxcomposite1 \
        libxdamage1 \
        libxrandr2 \
        libxtst6 \
        libxss1 \
        libxshmfence1 \
        libasound2t64 \
        libpulse0 \
        libfontconfig1 \
        libxi6 \
        libsm6 \
        libxrender1 \
        libxslt1.1 \
        libsqlite3-0 \
        libxkbcommon0 \
        libxcb-cursor0 \
        libxcb-xinerama0 \
        libxcb-icccm4 \
        libxcb-image0 \
        libxcb-keysyms1 \
        libxcb-randr0 \
        libxcb-render-util0 \
        libxcb-shape0 \
        libxcb-shm0 \
        libxcb-sync1 \
        libxcb-xfixes0 \
        libxcb-xtest0 \
        libxcb-xkb1 \
        libxcb-glx0 \
        libxcb-dri3-0 \
        libxcb-present0 \
        libglib2.0-0t64 \
        libdbus-1-3 \
        libcups2t64 \
        libatk-bridge2.0-0t64 \
        libwayland-client0 \
        libwayland-cursor0 \
        libwayland-egl1 \
        libva2 \
        libva-drm2 \
        libva-x11-2 \
        libpipewire-0.3-0t64 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Zoom client
RUN if [ -n "${ZOOM_DEB_URL}" ]; then \
        url="${ZOOM_DEB_URL}"; \
    else \
        url="https://zoom.us/client/${ZOOM_VERSION}/zoom_amd64.deb"; \
    fi && \
    echo "Downloading Zoom from ${url}" && \
    wget -q -O /tmp/zoom_amd64.deb "${url}" && \
    apt-get update && \
    apt-get install --no-install-recommends -y /tmp/zoom_amd64.deb && \
    rm -f /tmp/zoom_amd64.deb && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    dpkg-query -W -f='Installed Zoom ${Version}\n' zoom

# Check that no shared library Zoom needs is missing.
# Bundled libraries are found via LD_LIBRARY_PATH like the Zoom launcher does.
# Missing libraries of the main binary or the X11 platform plugin fail the build,
# anything else is only printed as a warning.
COPY res/check-zoom-libs.sh /tmp/check-zoom-libs.sh
RUN bash /tmp/check-zoom-libs.sh && rm -f /tmp/check-zoom-libs.sh

# Allow access to pulseaudio
RUN adduser zoomrec pulse-access

# Machine ID is kept in the persistent Zoom profile, see entrypoint.sh
RUN mkdir -p /var/lib/dbus && \
    ln -sf /home/zoomrec/.zoom/machine-id /etc/machine-id && \
    ln -sf /home/zoomrec/.zoom/machine-id /var/lib/dbus/machine-id

# Add home resources, startup scripts and python script with resources
COPY res/home/ ${HOME}/
COPY res/entrypoint.sh res/xfce.sh ${START_DIR}/
COPY zoomrec.py zoomrec_core.py transcription.py ${HOME}/

# Set permissions
RUN chmod a+x ${START_DIR}/*.sh && \
    chmod -R a+rw ${START_DIR} && \
    mkdir -p ${HOME}/recordings ${HOME}/audio ${HOME}/.zoom ${HOME}/config && \
    chown -R zoomrec:zoomrec ${HOME} && \
    find ${HOME}/ -name '*.sh' -exec chmod a+x {} + && \
    find ${HOME}/ -name '*.desktop' -exec chmod a+x {} +

EXPOSE ${VNC_PORT}
USER zoomrec
CMD ["/start/entrypoint.sh"]
