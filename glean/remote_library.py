"""Mac clients use the glean-server library.

Records live on glean-server. The Mac keeps PDFs under ~/Documents/glean/attachments.
When GLEAN_LIBRARY is unset on macOS, catalog commands run on glean-server over
SSH. Set GLEAN_LOCAL=1 to force the on-disk default library.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from ruamel.yaml import YAML

HOST = os.environ.get("GLEAN_HOST", "glean-server")
REMOTE_RECORDS = "/srv/glean/records"
LOCAL_PDF_ROOT = Path("~/Documents/glean").expanduser()

# These commands need a local file, a local editor, or a process on this Mac.
LOCAL_COMMANDS = frozenset({
    "open",
    "attach",
    "edit",
    "serve",
    "tui",
    "import",
    "reconcile-attachments",
})


def remote_catalog_enabled() -> bool:
    if os.environ.get("GLEAN_LIBRARY"):
        return False
    if os.environ.get("GLEAN_LOCAL") == "1":
        return False
    return sys.platform == "darwin"


def command_name(argv: list[str]) -> str | None:
    for arg in argv:
        if arg == "--":
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


def should_delegate(argv: list[str]) -> bool:
    if not remote_catalog_enabled():
        return False
    cmd = command_name(argv)
    if cmd is None or cmd in LOCAL_COMMANDS:
        return False
    return True


def remote_shell(script: str) -> list[str]:
    """One remote command. A login shell so `glean` is on PATH."""
    return [
        "ssh",
        "-o", "BatchMode=yes",
        HOST,
        f"bash -lc {shlex.quote(script)}",
    ]


def delegate(argv: list[str]) -> int:
    script = "glean " + shlex.join(argv)
    return subprocess.call(remote_shell(script))


def push_record_file(citekey: str, yaml_path: Path) -> int:
    """Install one record YAML on glean-server and refresh its index."""
    destination = f"{REMOTE_RECORDS}/{citekey}.yaml"
    copied = subprocess.call([
        "scp", "-o", "BatchMode=yes",
        str(yaml_path),
        f"{HOST}:{destination}",
    ])
    if copied != 0:
        return copied
    return subprocess.call(remote_shell("glean reindex"))


def fetch_record_text(citekey: str) -> str | None:
    path = f"{REMOTE_RECORDS}/{citekey}.yaml"
    result = subprocess.run(
        remote_shell("cat " + shlex.quote(path)),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout


def attachment_relpath(record_text: str, kind: str) -> str | None:
    data = YAML(typ="safe").load(record_text) or {}
    for item in data.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == kind and item.get("path"):
            return str(item["path"])
    return None


def local_attachment_path(relpath: str, root: Path = LOCAL_PDF_ROOT) -> Path | None:
    root = root.expanduser().resolve()
    target = (root / relpath).resolve()
    if not target.is_relative_to(root):
        return None
    return target
