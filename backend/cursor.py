"""Cursor management for incremental SSH sync.

Cursor files live at .cache/sources/{server_id}_cursor.json, separate
from the data cache so a data wipe doesn't lose sync progress.

Cursor shape:
{
  "messages": {"file_size": 45678, "synced_at": "2026-04-08T10:00:00Z"},
  "sessions": {
    "files": {"/home/user/.claude/projects/foo/abc.jsonl": 12345},
    "synced_at": "2026-04-08T10:00:00Z"
  },
  "plans": {
    "files": {"/home/user/.claude/plans/my-plan.md": 1744070400},
    "synced_at": "2026-04-08T10:00:00Z"
  }
}
"""
import json
import os
from pathlib import Path

_CACHE_DIR = Path(os.path.dirname(os.path.dirname(__file__))) / ".cache" / "sources"


def _cursor_path(server_id):
    return _CACHE_DIR / f"{server_id}_cursor.json"


def load_cursor(server_id):
    """Load sync cursor for a server. Returns {} if not found or corrupt."""
    path = _cursor_path(server_id)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_cursor(server_id, cursor):
    """Persist sync cursor for a server."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_cursor_path(server_id), "w") as f:
        json.dump(cursor, f, indent=2)


def clear_cursor(server_id):
    """Delete the sync cursor for a server (forces full sync next time)."""
    path = _cursor_path(server_id)
    if path.exists():
        path.unlink()
