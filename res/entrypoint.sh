#!/bin/bash
set -e

cleanup () {
    kill -s SIGTERM $! 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM

VNC_IP=$(hostname -i)
DISPLAY_NUM="${DISPLAY#:}"

# Mounted directories must be writable by the zoomrec user (uid 1000)
for dir in "$HOME/.zoom" "$HOME/recordings"; do
    if [[ -d "$dir" && ! -w "$dir" ]]; then
        echo "ERROR: $dir is not writable for uid $(id -u)." >&2
        echo "Run on the host: sudo chown -R 1000:1000 <mounted directory>" >&2
        exit 1
    fi
done

# Zoom profile: Zoom encrypts its settings with the device ID from zoomus.conf and
# may use the machine ID, so both are kept in the persistent profile directory.
PROFILE_DIR="$HOME/.zoom"
if [[ -w "$PROFILE_DIR" ]]; then
    if [[ -s "$PROFILE_DIR/zoomus.conf" ]]; then
        cp "$PROFILE_DIR/zoomus.conf" "$HOME/.config/zoomus.conf"
    else
        cp "$HOME/.config/zoomus.conf" "$PROFILE_DIR/zoomus.conf"
    fi
    if [[ ! -s "$PROFILE_DIR/machine-id" ]]; then
        tr -d '-' < /proc/sys/kernel/random/uuid > "$PROFILE_DIR/machine-id"
    fi
fi

# Change vnc password
mkdir -p "$HOME/.vnc"
PASSWD_PATH="$HOME/.vnc/passwd"
rm -f "$PASSWD_PATH"
echo "$VNC_PW" | vncpasswd -f > "$PASSWD_PATH"
chmod 600 "$PASSWD_PATH"

# Remove stale X locks from a previous run of this container
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"

echo -e "\nDISPLAY = $DISPLAY\nVNC_COL_DEPTH = $VNC_COL_DEPTH\nVNC_RESOLUTION = $VNC_RESOLUTION\nVNC_IP = $VNC_IP\nVNC_PORT = $VNC_PORT"

# Create X authority cookie (python-xlib/pyautogui require ~/.Xauthority)
export XAUTHORITY="$HOME/.Xauthority"
rm -f "$XAUTHORITY"
touch "$XAUTHORITY"
xauth -q add "$DISPLAY" . "$(mcookie)"

# Start the X server with built-in VNC server
Xtigervnc "$DISPLAY" \
    -auth "$XAUTHORITY" \
    -geometry "$VNC_RESOLUTION" \
    -depth "$VNC_COL_DEPTH" \
    -rfbport "$VNC_PORT" \
    -rfbauth "$PASSWD_PATH" \
    -SecurityTypes VncAuth \
    -localhost=0 \
    -AlwaysShared \
    -desktop zoomrec \
    &> "$START_DIR"/vnc_startup.log &

# Wait for X server
for _ in $(seq 1 30); do
    xdpyinfo -display "$DISPLAY" &> /dev/null && break
    sleep 1
done
if ! xdpyinfo -display "$DISPLAY" &> /dev/null; then
    echo "X server did not start:" >&2
    cat "$START_DIR"/vnc_startup.log >&2
    exit 1
fi

echo -e "\nConnect to $VNC_IP:$VNC_PORT"

# Start xfce4
"$START_DIR"/xfce.sh &> "$START_DIR"/xfce.log

# Cleanup to ensure pulseaudio is stateless
rm -rf /var/run/pulse /var/lib/pulse "$HOME"/.config/pulse

# Start audio
pulseaudio -D --exit-idle-time=-1 --log-level=error

# Keep sinks running when silent, otherwise the recording gets gaps
pactl unload-module module-suspend-on-idle 2>/dev/null || true

# Create speaker Dummy-Output (Zoom plays meeting audio here, FFmpeg records speaker.monitor)
# 48 kHz is what Zoom uses, so audio is not resampled
pactl load-module module-null-sink sink_name=speaker rate=48000 channels=2 sink_properties=device.description="speaker" > /dev/null
pactl set-source-volume speaker.monitor 100%

# Create microphone Dummy-Output
pactl load-module module-null-sink sink_name=microphone sink_properties=device.description="microphone" > /dev/null
pactl set-source-volume microphone.monitor 100%

# Map microphone-Output to microphone-Input
pactl load-module module-remap-source master=microphone.monitor source_name=microphone_in source_properties=device.description="microphone" > /dev/null
# Set microphone Volume
pactl set-source-volume microphone_in 60%

pactl set-default-sink speaker
pactl set-default-source microphone_in

echo -e "\nStart script.."
sleep 5

# Start python script in separated terminal
if [[ "$DEBUG" == "True" ]]; then
  # Wait if something failed
  xfce4-terminal -H --geometry 85x7+0 --title=zoomrec --hide-toolbar --hide-menubar --hide-scrollbar --hide-borders --zoom=-3 -e "python3 -u ${HOME}/zoomrec.py"
else
  # Exit container if something failed
  xfce4-terminal --geometry 85x7+0 --title=zoomrec --hide-toolbar --hide-menubar --hide-scrollbar --hide-borders --zoom=-3 -e "python3 -u ${HOME}/zoomrec.py"
fi
