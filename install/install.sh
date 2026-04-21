#!/bin/bash
# Install glean into its own pipx-managed venv and expose the CLI on PATH.
# Run from the repo root: ./install/install.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

command -v pipx >/dev/null || {
    echo "pipx not found. Install with:  brew install pipx  && pipx ensurepath"
    exit 1
}

PY_DEFAULT="$HOME/mamba/envs/meteo/bin/python"
PY="${PYTHON:-$PY_DEFAULT}"
[ -x "$PY" ] || { echo "Python not found at $PY; set PYTHON= or install it."; exit 1; }

"$PY" -c 'import sys; assert sys.version_info >= (3, 11), sys.version' \
    || { echo "Need Python 3.11+; got $("$PY" --version)"; exit 1; }

pipx install --python "$PY" --force -e "$REPO"
echo
echo "Done. Verify with:  glean --help"
