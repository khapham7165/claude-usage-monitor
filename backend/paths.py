"""Resolve where on-disk state (server configs, sync caches, cursors) lives.

Source mode (./start.sh) keeps state next to the repo so it's easy to inspect
and reset. Bundled mode (PyInstaller .app) puts state in the OS user-data dir
so a new bundle replacing the old one doesn't wipe it.
"""
import os
import shutil
import sys
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _is_frozen():
    """True when running inside a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def _user_data_dir():
    """OS-standard per-user data dir for this app."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ClaudeUsageMonitor"
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "ClaudeUsageMonitor"
    # Linux / other Unix
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "claude-usage-monitor"


def _source_dir():
    """The repo root when running from source. Two parents up from this file
    (backend/paths.py → backend/ → repo root)."""
    return Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def state_dir():
    """The directory all persistent state should live under.

    Created if missing. In bundled mode this is ~/Library/Application Support/
    ClaudeUsageMonitor (or platform equivalent); in source mode it's the repo
    root so developers can inspect/reset state in place.
    """
    if _is_frozen():
        d = _user_data_dir()
    else:
        d = _source_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


_MIGRATION_FLAG = "MIGRATED_FROM_BUNDLE"


def migrate_legacy_state():
    """One-shot copy of legacy state into the new location.

    Only relevant for bundled mode: if the user-data dir is empty AND a
    legacy state path (project-relative when previously bundled the data
    inside the .app) has data, copy it over. We leave the originals so the
    user can roll back to the old build without losing anything.
    """
    if not _is_frozen():
        return
    target = _user_data_dir()
    flag = target / f".{_MIGRATION_FLAG}"
    if flag.exists():
        return

    # In a frozen build, _source_dir() resolves to a path inside the bundle.
    # That's where the old build wrote state too — same code, same path.
    legacy_root = _source_dir()
    legacy_config = legacy_root / ".config.json"
    legacy_cache = legacy_root / ".cache"

    copied_any = False
    target.mkdir(parents=True, exist_ok=True)

    if legacy_config.is_file() and not (target / ".config.json").exists():
        try:
            shutil.copy2(legacy_config, target / ".config.json")
            copied_any = True
            log.warning("Migrated legacy .config.json from %s", legacy_config)
        except OSError as e:
            log.warning("Could not migrate %s: %s", legacy_config, e)

    if legacy_cache.is_dir() and not (target / ".cache").exists():
        try:
            shutil.copytree(legacy_cache, target / ".cache")
            copied_any = True
            log.warning("Migrated legacy .cache/ from %s", legacy_cache)
        except OSError as e:
            log.warning("Could not migrate %s: %s", legacy_cache, e)

    # Always drop the flag so we don't re-scan on every launch, even if nothing
    # was copied.
    try:
        flag.touch()
    except OSError:
        pass
    if copied_any:
        log.warning("Legacy state migration complete — originals left at %s", legacy_root)
