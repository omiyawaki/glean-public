#!/bin/bash
# Install glean in the shared Pixi tools environment and verify the CLI.
# Run from the repo root: ./install/install.sh
set -euo pipefail
MANIFEST="$HOME/tools/bringup/python-env"
RUNNER_INSTALLER="$HOME/tools/bringup/bin/install-tools-python-runner"
PLIST_SOURCE="$HOME/tools/glean/install/com.miyawaki.glean-web.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.miyawaki.glean-web.plist"
DOMAIN="gui/$(id -u)"
HOST="$(/usr/sbin/scutil --get LocalHostName 2>/dev/null || /bin/hostname -s)"

command -v pixi >/dev/null || {
    echo "Pixi not found on PATH. Install it with: brew install pixi"
    exit 1
}

[ -f "$MANIFEST/pixi.toml" ] || {
    echo "Shared Pixi workspace not found at $MANIFEST"
    exit 1
}

pixi install --manifest-path "$MANIFEST" --environment tools --locked
pixi run --manifest-path "$MANIFEST" --environment tools \
    python -m playwright install chromium
pixi run --manifest-path "$MANIFEST" --environment tools glean --help >/dev/null

case "$HOST" in
    home) ;;
    work)
        launchctl bootout "$DOMAIN" "$PLIST_DEST" 2>/dev/null || true
        launchctl disable "$DOMAIN/com.miyawaki.glean-web" 2>/dev/null || true
        rm -f "$PLIST_DEST"
        echo "Glean web migrated to home; stale LaunchAgent disabled."
        exit 0
        ;;
    *)
        echo "Glean CLI installed; no Glean web LaunchAgent role for host $HOST."
        exit 0
        ;;
esac

[ -x "$RUNNER_INSTALLER" ] || {
    echo "Tools Python Runner installer not found at $RUNNER_INSTALLER"
    exit 1
}
"$RUNNER_INSTALLER" >/dev/null
if ! "$HOME/tools/bringup/bin/check-python-runtime-migration" --privacy-probe-only; then
    echo "Grant Full Disk Access to $HOME/Applications/ToolsPythonRunner.app, then rerun." >&2
    exit 1
fi
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$PLIST_SOURCE" "$PLIST_DEST"
launchctl bootout "$DOMAIN" "$PLIST_DEST" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST_DEST"
launchctl enable "$DOMAIN/com.miyawaki.glean-web"
echo
echo "Done. Verify with: glean --help"
echo "LaunchAgent: $PLIST_DEST"
