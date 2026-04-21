#!/bin/bash
# Install glean: pip-install the package, symlink the CLI onto PATH.
# Run from the repo root: ./install/install.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

echo "Repo: $REPO"

PY="${PYTHON:-python3}"
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), sys.version' \
    || { echo "Need Python 3.11+"; exit 1; }

# 1) Editable install — package becomes importable, `glean` entrypoint gets written.
"$PY" -m pip install --user -e "$REPO"

# 2) Symlink the entrypoint into ~/.local/bin per the tools-dir convention.
#    pip --user writes the script to the user scheme bindir; resolve it and symlink.
USER_BIN="$("$PY" -c 'import sysconfig; print(sysconfig.get_path("scripts", scheme="posix_user"))')"
SRC="$USER_BIN/glean"
DEST="$HOME/.local/bin/glean"
mkdir -p "$HOME/.local/bin"
if [ ! -e "$SRC" ]; then
    echo "Expected entrypoint at $SRC but it is missing. Check pip install output."
    exit 1
fi
if [ -L "$DEST" ] || [ -e "$DEST" ]; then
    rm "$DEST"
fi
ln -s "$SRC" "$DEST"
echo "Symlinked $DEST -> $SRC"

echo
echo "Done. Verify with:  glean --help"
echo "If not on PATH, add ~/.local/bin to PATH in your shell rc."
