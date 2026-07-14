#!/bin/bash
# Install glean in the shared Pixi tools environment and verify the CLI.
# Run from the repo root: ./install/install.sh
set -euo pipefail
MANIFEST="$HOME/tools/bringup/python-env"

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
echo
echo "Done. Verify with:  glean --help"
