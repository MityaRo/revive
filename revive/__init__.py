import json
import os
from pathlib import Path

HOME = Path.home() / ".revive"
SNAPSHOTS = HOME / "snapshots"
SESSION_MAP = HOME / "sessions"  # written by the Claude Code hook, keyed by iTerm2 session UUID

GUESS_SOURCES = {"transcript", "transcript-newest"}


def write_json(path: Path, data: dict) -> None:
    """Write through a temp file so a concurrent reader never sees half a document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def panes(snap: dict) -> list[dict]:
    """Every leaf pane in a snapshot, in tab order."""
    return [p for w in snap["windows"] for t in w["tabs"] for p in leaves(t["root"])]


def leaves(node: dict) -> list[dict]:
    return [node] if "children" not in node else [p for c in node["children"] for p in leaves(c)]
