"""Collect Claude usage data from remote servers via SSH."""
import os
import json
from datetime import datetime, timezone
import paramiko
from backend.parsers import _parse_history_lines, _parse_token_log_lines, _decode_project_path
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
        if jsonl_files:
            token_logs = _read_project_files_batched(client, jsonl_files, claude_dir, source, _progress)

        _progress("done", f"{len(history)} msgs, {len(token_logs)} token logs")

        return {
            "success": True,
            "server_id": server_id,
            "history": history,
            "token_logs": token_logs,
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
    """Read project .jsonl files in batches via exec_command to avoid fd exhaustion."""
    token_logs = []
    projects_prefix = f"{claude_dir}/projects/"
    total = len(jsonl_files)
    batch_size = 50  # files per batch

    for batch_start in range(0, total, batch_size):
        batch = jsonl_files[batch_start:batch_start + batch_size]
        batch_end = min(batch_start + batch_size, total)
        progress_cb("reading_projects", f"Reading files {batch_start + 1}-{batch_end} of {total}")

        # Use a delimiter-separated concat of all files in the batch
        # Each file output is wrapped with markers so we can split
        parts = []
        for fpath in batch:
            # Echo a JSON header line, then the file content
            safe_path = fpath.replace("'", "'\\''")
            parts.append(f"echo '<<<FILE:{fpath}>>>'; cat '{safe_path}' 2>/dev/null; echo '<<<END>>>'")
        cmd = "; ".join(parts)

        raw = _exec(client, cmd)

        # Parse the concatenated output
        current_file = None
        current_lines = []
        for line in raw.splitlines():
            if line.startswith("<<<FILE:") and line.endswith(">>>"):
                # Flush previous file
                if current_file and current_lines:
                    _process_file(current_file, current_lines, projects_prefix, source, token_logs)
                current_file = line[8:-3]  # extract path
                current_lines = []
            elif line == "<<<END>>>":
                if current_file and current_lines:
                    _process_file(current_file, current_lines, projects_prefix, source, token_logs)
                current_file = None
                current_lines = []
            elif current_file:
                current_lines.append(line)

        # Flush last file
        if current_file and current_lines:
            _process_file(current_file, current_lines, projects_prefix, source, token_logs)

    return token_logs


def _process_file(file_path, lines, projects_prefix, source, token_logs):
    """Parse a single project session file's lines into token logs."""
    # Extract project name and session id from path
    # Path: /home/user/.claude/projects/project-name/session-id.jsonl
    rel = file_path
    if projects_prefix in file_path:
        rel = file_path.split(projects_prefix, 1)[1]

    parts = rel.split("/")
    if len(parts) >= 2:
        proj_name = parts[0]
        session_id = parts[-1].replace(".jsonl", "")
        project_path = _decode_project_path(proj_name)
        token_logs.extend(_parse_token_log_lines(lines, project_path, session_id, source=source))
