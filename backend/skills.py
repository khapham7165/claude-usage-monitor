"""Skill discovery — enumerate SKILL.md files from user, plugin, and project sources."""
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_HOME = Path.home()
_USER_SKILLS = _HOME / ".claude" / "skills"
_PROJECTS_DIR = _HOME / ".claude" / "projects"
_PLUGINS_INSTALLED = _HOME / ".claude" / "plugins" / "installed_plugins.json"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_CWD_RE = re.compile(r'"cwd":"([^"]+)"')
_MAX_READ_BYTES = 64 * 1024


def _parse_frontmatter(text):
    """Extract name + description from YAML-ish frontmatter. Minimal parser
    (no PyYAML dep); handles `key: value` and `key: "quoted value"`."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    body_start = m.end()
    fm = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if v.startswith('"') and v.endswith('"') and len(v) >= 2:
            v = v[1:-1]
        fm[k] = v
    return fm, text[body_start:]


def _read_skill_md(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(_MAX_READ_BYTES)
    except OSError as e:
        log.warning("cannot read %s: %s", path, e)
        return ""


def _build_entry(skill_md_path, source, scope_root):
    text = _read_skill_md(skill_md_path)
    fm, _ = _parse_frontmatter(text)
    folder = skill_md_path.parent
    return {
        "name": fm.get("name") or folder.name,
        "description": fm.get("description") or "",
        "source": source,
        "path": str(skill_md_path),
        "folder": str(folder),
        "scope_root": str(scope_root),
    }


def _scan_dir_for_skills(root, source, scope_root):
    """Find SKILL.md files in <root>/*/SKILL.md and (root)/SKILL.md."""
    out = []
    if not root.exists() or not root.is_dir():
        return out
    direct = root / "SKILL.md"
    if direct.is_file():
        out.append(_build_entry(direct, source, scope_root))
    try:
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            sm = child / "SKILL.md"
            if sm.is_file():
                out.append(_build_entry(sm, source, scope_root))
    except OSError as e:
        log.warning("cannot list %s: %s", root, e)
    return out


def _known_project_dirs():
    """Decode project paths from ~/.claude/projects/* using the cwd field
    stored in the JSONL session files. Skips dirs whose cwd no longer exists."""
    dirs = set()
    if not _PROJECTS_DIR.is_dir():
        return []
    for d in _PROJECTS_DIR.iterdir():
        if not d.is_dir():
            continue
        try:
            jsonl = next(d.glob("*.jsonl"), None)
        except OSError:
            continue
        if not jsonl:
            continue
        try:
            with open(jsonl, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = _CWD_RE.search(line)
                    if m:
                        dirs.add(m.group(1))
                        break
        except OSError:
            continue
    return [p for p in sorted(dirs) if os.path.isdir(p)]


def _plugin_skill_roots():
    """For each installed plugin, return (label, install_path) candidates that
    may contain skills — both the plugin root (single-skill plugin) and a
    `skills/` subfolder."""
    roots = []
    if not _PLUGINS_INSTALLED.is_file():
        return roots
    try:
        data = json.loads(_PLUGINS_INSTALLED.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("cannot read installed_plugins.json: %s", e)
        return roots
    for plugin_key, versions in (data.get("plugins") or {}).items():
        if not versions:
            continue
        latest = versions[-1]
        install_path = latest.get("installPath")
        if not install_path:
            continue
        root = Path(install_path)
        label = plugin_key.split("@")[0]
        roots.append((label, root, root))
        roots.append((label, root / "skills", root))
    return roots


def _local_skills():
    out = []
    for entry in _scan_dir_for_skills(_USER_SKILLS, "user", _USER_SKILLS):
        entry["_source"] = "local"
        out.append(entry)
    for label, root, scope_root in _plugin_skill_roots():
        for entry in _scan_dir_for_skills(root, f"plugin:{label}", scope_root):
            entry["_source"] = "local"
            out.append(entry)
    for project_dir in _known_project_dirs():
        project_skills = Path(project_dir) / ".claude" / "skills"
        for entry in _scan_dir_for_skills(project_skills, "project", Path(project_dir)):
            entry["project"] = project_dir
            entry["_source"] = "local"
            out.append(entry)
    return out


def _remote_skill_descriptors():
    """Map cached remote skill entries (which carry inline body) into the same
    shape as local descriptors. The body stays in the dict so the content
    endpoint can serve it without another SSH call."""
    from backend import aggregators
    out = []
    for s in aggregators.remote_skills():
        out.append({
            "name": s.get("name") or "",
            "description": s.get("description") or "",
            "source": s.get("source_kind") or "unknown",
            "path": s.get("path") or "",
            "folder": s.get("folder") or "",
            "scope_root": s.get("scope_root") or "",
            "project": s.get("project"),
            "_source": s.get("_source") or "",
            "_body": s.get("body") or "",
        })
    return out


def list_skills(source=None):
    """Return a flat list of skill descriptors.

    source=None     → local skills + all cached remote skills
    source='local'  → local only
    source='ssh:X'  → only that server's cached skills
    """
    if source == "local":
        return _local_skills()

    if source and source.startswith("ssh:"):
        return [s for s in _remote_skill_descriptors() if s["_source"] == source]

    # Default: merged view
    out = _local_skills()
    out.extend(_remote_skill_descriptors())
    return out


def _allowed_roots():
    """Paths under which read/open is permitted."""
    roots = [_USER_SKILLS]
    for _, root, _scope in _plugin_skill_roots():
        roots.append(root)
    for project_dir in _known_project_dirs():
        roots.append(Path(project_dir) / ".claude" / "skills")
    return roots


def _is_under_allowed_root(path):
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    for root in _allowed_roots():
        try:
            resolved.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def read_skill_content(path, source=None):
    """Read a SKILL.md and split out the frontmatter.

    For local skills the path must live under a known skill root; for remote
    skills the body is already cached during sync, so we look it up by path
    in the cached remote_skills.
    """
    if source and source.startswith("ssh:"):
        from backend import aggregators
        for s in aggregators.remote_skills():
            if s.get("_source") == source and s.get("path") == path:
                return {
                    "frontmatter": {
                        "name": s.get("name", ""),
                        "description": s.get("description", ""),
                    },
                    "body": s.get("body", ""),
                    "path": path,
                }
        return {"error": "Remote skill not found in cache — try syncing again"}

    if not _is_under_allowed_root(path):
        return {"error": "Path is not within a known skill root"}
    p = Path(path)
    if not p.is_file():
        return {"error": "File not found"}
    text = _read_skill_md(p)
    fm, body = _parse_frontmatter(text)
    return {"frontmatter": fm, "body": body, "path": str(p)}


def open_in_editor(path):
    """Open path in `code` if available, else with the OS default handler."""
    if not _is_under_allowed_root(path):
        return {"error": "Path is not within a known skill root"}
    if not Path(path).exists():
        return {"error": "Path not found"}
    cmd = "code" if shutil.which("code") else "open"
    try:
        subprocess.Popen([cmd, str(path)])
    except OSError as e:
        return {"error": f"Failed to launch editor: {e}"}
    return {"ok": True, "command": cmd}
