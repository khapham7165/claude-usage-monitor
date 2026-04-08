import subprocess
import json
from backend.parsers import parse_sessions_metadata


def get_local_active_sessions():
    """Detect locally running Claude Code processes."""
    try:
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5
        )
        running_pids = set()
        for line in result.stdout.splitlines():
            if "claude" in line.lower() and "python" not in line.lower():
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        running_pids.add(int(parts[1]))
                    except ValueError:
                        continue
    except Exception:
        running_pids = set()

    sessions = parse_sessions_metadata()
    active = []
    for session in sessions:
        pid = session.get("pid")
        if pid in running_pids:
            active.append({
                "pid": pid,
                "sessionId": session.get("sessionId", ""),
                "cwd": session.get("cwd", ""),
                "startedAt": session.get("startedAt", 0),
                "kind": session.get("kind", ""),
                "entrypoint": session.get("entrypoint", ""),
                "_source": "local",
            })

    return active


def get_remote_active_sessions(server_config):
    """Detect running Claude processes on a remote server via SSH."""
    from backend.ssh_collector import _connect, _exec

    server_id = server_config["id"]
    source = f"ssh:{server_id}"

    client = None
    try:
        client = _connect(server_config)
        home = _exec(client, "echo $HOME").strip()
        claude_dir = f"{home}/.claude"

        # Get running claude PIDs
        ps_out = _exec(client, "ps aux 2>/dev/null | grep -i claude | grep -v grep | grep -v python")
        running_pids = set()
        for line in ps_out.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                try:
                    running_pids.add(int(parts[1]))
                except ValueError:
                    continue

        # Read session metadata
        sessions_raw = _exec(client, f"for f in {claude_dir}/sessions/*.json; do [ -f \"$f\" ] && cat \"$f\" && echo '<<<SEP>>>'; done 2>/dev/null")
        sessions = []
        for chunk in sessions_raw.split("<<<SEP>>>"):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                sessions.append(json.loads(chunk))
            except json.JSONDecodeError:
                continue

        active = []
        for session in sessions:
            pid = session.get("pid")
            if pid in running_pids:
                active.append({
                    "pid": pid,
                    "sessionId": session.get("sessionId", ""),
                    "cwd": session.get("cwd", ""),
                    "startedAt": session.get("startedAt", 0),
                    "kind": session.get("kind", ""),
                    "entrypoint": session.get("entrypoint", ""),
                    "_source": source,
                    "_server_id": server_id,
                    "_host": server_config.get("host", ""),
                })

        return active
    except Exception:
        return []
    finally:
        if client:
            client.close()


def get_active_sessions(source=None):
    """Get all active sessions (local + remote), optionally filtered by source."""
    from backend.ssh_collector import list_servers

    all_sessions = []

    # Local
    if not source or source == "local" or source == "all" or source == "":
        all_sessions.extend(get_local_active_sessions())

    # Remote — only if we're asking for all or a specific SSH source
    if not source or source == "" or source == "all" or (source and source.startswith("ssh:")):
        for srv in list_servers():
            srv_source = f"ssh:{srv['id']}"
            if source and source != "" and source != "all" and source != srv_source:
                continue
            all_sessions.extend(get_remote_active_sessions(srv))

    return all_sessions
