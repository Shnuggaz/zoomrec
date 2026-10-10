#!/bin/bash
# Report shared libraries that Zoom needs but that are not installed.
set -euo pipefail

ZOOM_DIR=/opt/zoom
LD_LIBRARY_PATH=$(find "$ZOOM_DIR" -name '*.so*' -printf '%h\n' | sort -u | paste -sd:)
export LD_LIBRARY_PATH

missing_in() {
    ldd "$1" 2>/dev/null | awk '/not found/ {print $1}' || true
}

critical=0
for f in "$ZOOM_DIR/zoom" $(find "$ZOOM_DIR" -name 'libqxcb.so*'); do
    libs=$(missing_in "$f")
    if [ -n "$libs" ]; then
        echo "ERROR: $f is missing:" $libs
        critical=1
    fi
done

warnings=$(find "$ZOOM_DIR" -type f \( -name '*.so*' -o -perm -u+x \) -print0 \
    | xargs -0 -n 50 ldd 2>/dev/null | awk '/not found/ {print $1}' | sort -u || true)
if [ -n "$warnings" ]; then
    echo "WARNING: libraries not found for some Zoom components (may be optional):"
    echo "$warnings" | sed 's/^/  /'
fi

if [ "$critical" -ne 0 ]; then
    echo "Add the packages providing the libraries above to the Dockerfile." >&2
    exit 1
fi
echo "Zoom library check passed."
