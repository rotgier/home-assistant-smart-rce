"""Which smart_rce code is running — git commit of the checkout, read once at setup.

Archived records carry it so a replay mismatch can be pinned to a version.
On HA the integration is a symlink into a git clone under `/share`; the walk
starts from the resolved file, so it finds that clone's `.git`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

_MAX_LEVELS_UP: Final = 5
_UNKNOWN: Final = "unknown"


def read_code_version(start: Path | None = None) -> str:
    """Short commit SHA of the enclosing git checkout, or "unknown". Blocking I/O."""
    here = (start or Path(__file__)).resolve()
    for directory in list(here.parents)[:_MAX_LEVELS_UP]:
        git_dir = directory / ".git"
        if git_dir.is_dir():
            return _resolve_head(git_dir) or _UNKNOWN
    return _UNKNOWN


def _resolve_head(git_dir: Path) -> str | None:
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("ref: "):
        return head[:12]  # detached HEAD holds the SHA itself
    ref = head.removeprefix("ref: ")
    loose = git_dir / ref
    if loose.is_file():
        return loose.read_text(encoding="utf-8").strip()[:12]
    packed = git_dir / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            sha, _, name = line.partition(" ")
            if name == ref:
                return sha[:12]
    return None
