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
PIPX_HOME="$(pipx environment --value PIPX_HOME 2>/dev/null || true)"
GLEAN_PY="$PIPX_HOME/venvs/glean/bin/python"
if [ -x "$GLEAN_PY" ]; then
    "$GLEAN_PY" -m playwright install chromium
else
    echo "Could not locate the glean pipx venv; run 'python -m playwright install chromium' inside it before PDF fetching."
fi
echo
echo "Done. Verify with:  glean --help"
