"""Collect Claude usage data from remote servers via SSH."""
import os
import json
from datetime import datetime, timezone
import paramiko
from backend.parsers import (
    _parse_history_lines, _parse_token_log_lines, _decode_project_path,
    _extract_annotations_from_lines,
)
from backend.auth import _load_config, _save_config, generate_id


# ── Server config CRUD ───────────────────────────────────────

def list_servers():
    config = _load_config()
    return config.get("ssh_servers", [])


def get_server(server_id):
    for srv in list_servers():
        if srv["id"] == server_id:
            return srv
    return None


def save_server(server_dict):
    config = _load_config()
    servers = config.setdefault("ssh_servers", [])
    if "id" not in server_dict:
        server_dict["id"] = generate_id("srv")
    for i, srv in enumerate(servers):
        if srv["id"] == server_dict["id"]:
            servers[i] = server_dict
            _save_config(config)
            return server_dict
    servers.append(server_dict)
    _save_config(config)
    return server_dict


def delete_server(server_id):
    config = _load_config()
    config["ssh_servers"] = [s for s in config.get("ssh_servers", []) if s["id"] != server_id]
    _save_config(config)


# ── SSH connection ───────────────────────────────────────────

def _connect(server_config):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    key_path = os.path.expanduser(server_config.get("key_path", "~/.ssh/id_rsa"))
    connect_kwargs = {
        "hostname": server_config["host"],
        "username": server_config.get("user", "root"),
        "port": server_config.get("port", 22),
        "timeout": 10,
    }

    if os.path.exists(key_path):
        connect_kwargs["key_filename"] = key_path
    else:
        connect_kwargs["allow_agent"] = True

    client.connect(**connect_kwargs)
    return client


def _exec(client, cmd):
    """Run a command over SSH and return stdout as string."""
    _, stdout, stderr = client.exec_command(cmd, timeout=30)
    try:
        return stdout.read().decode("utf-8", errors="replace")
    finally:
        stdout.close()
        stderr.close()


def test_connection(server_config):
    """Test SSH connection and verify ~/.claude/ exists."""
    client = None
    try:
        client = _connect(server_config)
        home = _exec(client, "echo $HOME").strip()
        claude_dir = f"{home}/.claude"

        check = _exec(client, f"test -d {claude_dir} && echo yes || echo no").strip()
        if check != "yes":
            return {"success": False, "error": f"~/.claude/ not found at {claude_dir}"}

        has_history = _exec(client, f"test -f {claude_dir}/history.jsonl && echo yes || echo no").strip() == "yes"
        return {"success": True, "home": home, "claude_dir": claude_dir, "has_history": has_history}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if client:
            client.close()


# ── Data sync (uses exec_command, not per-file SFTP) ─────────

def sync_server(server_config, progress_cb=None):
    """Fetch all Claude usage data from a remote server."""
    server_id = server_config["id"]
    source = f"ssh:{server_id}"

    def _progress(step, detail=""):
        if progress_cb:
            progress_cb(step, detail)

    client = None
    try:
        _progress("connecting", f"{server_config.get('user', 'root')}@{server_config['host']}")
        client = _connect(server_config)

        _progress("discovering", "Locating ~/.claude/")
        home = _exec(client, "echo $HOME").strip()
        claude_dir = f"{home}/.claude"

        # Read history.jsonl in one shot
        _progress("reading_history", "Reading history.jsonl")
        history_raw = _exec(client, f"cat {claude_dir}/history.jsonl 2>/dev/null")
        history = _parse_history_lines(history_raw.splitlines(), source=source)
        _progress("reading_history_done", f"{len(history)} messages")

        # Discover project session files
        _progress("reading_projects", "Listing project files")
        file_list_raw = _exec(client, f"find {claude_dir}/projects -name '*.jsonl' -type f 2>/dev/null")
        jsonl_files = [f.strip() for f in file_list_raw.splitlines() if f.strip()]
        _progress("reading_projects", f"Found {len(jsonl_files)} session files")

        # Read all files in batches using a single tar stream to avoid too-many-open-files
        token_logs = []
        session_plans = []
        session_tasks = {}
        if jsonl_files:
            token_logs, session_plans, session_tasks = _read_project_files_batched(
                client, jsonl_files, claude_dir, source, _progress
            )

        # Read remote plan files
        _progress("reading_plans", "Reading plan files")
        plans = _read_remote_plans(client, claude_dir, source)
        _progress("reading_plans_done", f"{len(plans)} plans")

        _progress("done", f"{len(history)} msgs, {len(token_logs)} token logs, {len(plans)} plans")

        return {
            "success": True,
            "server_id": server_id,
            "history": history,
            "token_logs": token_logs,
            "session_plans": session_plans,
            "session_tasks": session_tasks,
            "plans": plans,
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "history_count": len(history),
            "token_log_count": len(token_logs),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if client:
            client.close()


def _read_project_files_batched(client, jsonl_files, claude_dir, source, progress_cb):
    """Read project .jsonl files in batches via exec_command to avoid fd exhaustion.
    Returns (token_logs, session_plans, session_tasks).
    """
    token_logs = []
    session_plans = []
    session_tasks = {}
    projects_prefix = f"{claude_dir}/projects/"
    total = len(jsonl_files)
    batch_size = 50  # files per batch

    for batch_start in range(0, total, batch_size):
        batch = jsonl_files[batch_start:batch_start + batch_size]
        batch_end = min(batch_start + batch_size, total)
        progress_cb("reading_projects", f"Reading files {batch_start + 1}-{batch_end} of {total}")

        parts = []
        for fpath in batch:
            safe_path = fpath.replace("'", "'\\''")
            parts.append(f"echo '<<<FILE:{fpath}>>>'; cat '{safe_path}' 2>/dev/null; echo '<<<END>>>'")
        cmd = "; ".join(parts)

        raw = _exec(client, cmd)

        current_file = None
        current_lines = []
        for line in raw.splitlines():
            if line.startswith("<<<FILE:") and line.endswith(">>>"):
                if current_file and current_lines:
                    _process_file(current_file, current_lines, projects_prefix, source,
                                  token_logs, session_plans, session_tasks)
                current_file = line[8:-3]
                current_lines = []
            elif line == "<<<END>>>":
                if current_file and current_lines:
                    _process_file(current_file, current_lines, projects_prefix, source,
                                  token_logs, session_plans, session_tasks)
                current_file = None
                current_lines = []
            elif current_file:
                current_lines.append(line)

        if current_file and current_lines:
            _process_file(current_file, current_lines, projects_prefix, source,
                          token_logs, session_plans, session_tasks)

    return token_logs, session_plans, session_tasks


def _process_file(file_path, lines, projects_prefix, source, token_logs, session_plans, session_tasks):
    """Parse a single project session file's lines into token logs and annotations."""
    rel = file_path
    if projects_prefix in file_path:
        rel = file_path.split(projects_prefix, 1)[1]

    parts = rel.split("/")
    if len(parts) >= 2:
        proj_name = parts[0]
        session_id = parts[-1].replace(".jsonl", "")
        project_path = _decode_project_path(proj_name)
        token_logs.extend(_parse_token_log_lines(lines, project_path, session_id, source=source))
        sp_entries, tasks = _extract_annotations_from_lines(lines, session_id, source=source)
        session_plans.extend(sp_entries)
        if tasks:
            session_tasks[session_id] = list(tasks.values())


def _read_remote_plans(client, claude_dir, source):
    """Read remote ~/.claude/plans/*.md files and return plan metadata list."""
    # List plan files
    raw = _exec(client, f"find {claude_dir}/plans -name '*.md' -type f 2>/dev/null")
    plan_files = [f.strip() for f in raw.splitlines() if f.strip().endswith(".md")]
    if not plan_files:
        return []

    # Read each plan with mtime and content using markers
    parts = []
    for fpath in plan_files:
        safe_path = fpath.replace("'", "'\\''")
        # Try Linux stat first, fall back to macOS stat
        parts.append(
            f"echo '<<<PLAN:{fpath}>>>';"
            f"(stat -c '%Y' '{safe_path}' 2>/dev/null || stat -f '%m' '{safe_path}' 2>/dev/null || echo 0);"
            f"cat '{safe_path}' 2>/dev/null;"
            f"echo '<<<END>>>'"
        )
    cmd = "; ".join(parts)
    raw = _exec(client, cmd)

    plans = []
    current_file = None
    current_mtime = None
    current_lines = []

    for line in raw.splitlines():
        if line.startswith("<<<PLAN:") and line.endswith(">>>"):
            if current_file is not None:
                _parse_remote_plan(current_file, current_mtime, current_lines, plans, source)
            current_file = line[8:-3]
            current_mtime = None
            current_lines = []
        elif line == "<<<END>>>":
            if current_file is not None:
                _parse_remote_plan(current_file, current_mtime, current_lines, plans, source)
            current_file = None
            current_mtime = None
            current_lines = []
        elif current_file is not None:
            if current_mtime is None and line.strip().isdigit():
                current_mtime = int(line.strip())
            else:
                current_lines.append(line)

    if current_file is not None:
        _parse_remote_plan(current_file, current_mtime, current_lines, plans, source)

    return plans


def _parse_remote_plan(file_path, mtime, lines, plans, source):
    """Append a parsed plan entry to the plans list."""
    from pathlib import PurePosixPath
    slug = PurePosixPath(file_path).stem
    content = "\n".join(lines)
    title = slug
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            break
    created_at = (
        datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        if mtime else ""
    )
    plans.append({
        "slug": slug,
        "title": title,
        "content": content,
        "preview": content[:300].rstrip(),
        "createdAt": created_at,
        "filePath": file_path,
        "_source": source,
    })
