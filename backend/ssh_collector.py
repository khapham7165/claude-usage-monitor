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


# ── Credential file candidates (probed in order) ────────────
_REMOTE_CRED_CANDIDATES = [
    "{home}/.claude/.credentials.json",
    "{home}/.config/Claude/.credentials.json",
]


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

def sync_server(server_config, progress_cb=None, cursor=None):
    """Fetch Claude usage data from a remote server.

    cursor: dict loaded via cursor.load_cursor(). Empty dict or None → full sync.
            Non-empty → incremental sync using per-type byte/size/mtime cursors.
    Returns dict with success, data, and new_cursor (to be saved by caller).
    """
    if cursor is None:
        cursor = {}

    server_id = server_config["id"]
    source = f"ssh:{server_id}"
    incremental = bool(cursor)

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

        # Read account + model while the connection is already open (no extra SSH connect)
        _raw_info = _read_account_and_model(client, home)
        active_account = {
            "has_credentials": _raw_info.get("has_credentials", False),
            "org_uuid": _raw_info.get("org_uuid", ""),
            "cred_path": _raw_info.get("cred_path"),
        }
        active_model = _raw_info.get("model", "claude-sonnet-4-6")

        msg_cursor = cursor.get("messages", {})
        sess_cursor = cursor.get("sessions", {})
        plan_cursor = cursor.get("plans", {})

        # Sync messages (history.jsonl)
        mode = "incremental" if msg_cursor else "full"
        _progress("reading_history", f"Reading history.jsonl ({mode})")
        history, new_msg_cursor, history_is_full = _sync_messages(
            client, claude_dir, source, msg_cursor
        )
        _progress("reading_history_done", f"{len(history)} messages")

        # Sync sessions (projects/**/*.jsonl)
        token_logs, session_plans, session_tasks, new_sess_cursor = _sync_sessions(
            client, claude_dir, source, sess_cursor, _progress
        )

        # Sync plans (plans/*.md)
        mode = "incremental" if plan_cursor else "full"
        _progress("reading_plans", f"Reading plan files ({mode})")
        plans, new_plan_cursor = _sync_plans(client, claude_dir, source, plan_cursor)
        _progress("reading_plans_done", f"{len(plans)} plans")

        _progress("done", f"{len(history)} msgs, {len(token_logs)} token logs, {len(plans)} plans")

        return {
            "success": True,
            "server_id": server_id,
            "incremental": incremental,
            "history_is_full": history_is_full,
            "history": history,
            "token_logs": token_logs,
            "session_plans": session_plans,
            "session_tasks": session_tasks,
            "plans": plans,
            "active_account": active_account,
            "active_model": active_model,
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "history_count": len(history),
            "token_log_count": len(token_logs),
            "new_cursor": {
                "messages": new_msg_cursor,
                "sessions": new_sess_cursor,
                "plans": new_plan_cursor,
            },
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if client:
            client.close()


# ── Incremental sync helpers ─────────────────────────────────

def _sync_messages(client, claude_dir, source, msg_cursor):
    """Fetch new messages from history.jsonl using a byte-offset cursor.

    Returns (records, new_cursor_fragment, history_is_full).
    history_is_full=True means records contain ALL history (replace stored),
    history_is_full=False means records are new-only (append to stored).
    """
    prev_size = msg_cursor.get("file_size", 0)

    # Get current file size and new content in one SSH round-trip.
    # tail -c +N uses 1-based offsets: +1 = whole file, +(prev+1) = new bytes only.
    cmd = (
        f"SIZE=$(wc -c < {claude_dir}/history.jsonl 2>/dev/null || echo 0); "
        f"echo \"SIZE:$SIZE\"; "
        f"tail -c +{prev_size + 1} {claude_dir}/history.jsonl 2>/dev/null"
    )
    raw = _exec(client, cmd)
    lines = raw.splitlines()

    new_size = prev_size
    content_start = 0
    if lines and lines[0].startswith("SIZE:"):
        try:
            new_size = int(lines[0][5:].strip())
        except ValueError:
            pass
        content_start = 1

    now = datetime.now(timezone.utc).isoformat()

    # No change
    if new_size == prev_size:
        return [], {"file_size": new_size, "synced_at": now}, False

    # File was truncated (rare) — do a full re-fetch so stored data is replaced
    if new_size < prev_size:
        full_raw = _exec(client, f"cat {claude_dir}/history.jsonl 2>/dev/null")
        records = _parse_history_lines(full_raw.splitlines(), source=source)
        return records, {"file_size": new_size, "synced_at": now}, True

    records = _parse_history_lines(lines[content_start:], source=source)
    return records, {"file_size": new_size, "synced_at": now}, False


def _sync_sessions(client, claude_dir, source, sess_cursor, progress_cb):
    """Sync project session files incrementally using per-file size cursors.

    Returns (token_logs, session_plans, session_tasks, new_cursor_fragment).
    """
    file_cursor = sess_cursor.get("files", {})

    # Get file list with sizes in one shot (size TAB path per line)
    progress_cb("reading_projects", "Listing project files")
    raw = _exec(
        client,
        f"find {claude_dir}/projects -name '*.jsonl' -type f 2>/dev/null"
        f" | while IFS= read -r f; do"
        f"   printf '%s\\t%s\\n' \"$(wc -c < \"$f\" 2>/dev/null || echo 0)\" \"$f\";"
        f" done",
    )

    files_with_sizes = {}  # path → current size
    for line in raw.splitlines():
        if "\t" in line:
            size_str, fpath = line.split("\t", 1)
            try:
                files_with_sizes[fpath.strip()] = int(size_str.strip())
            except ValueError:
                files_with_sizes[fpath.strip()] = 0

    new_files = []
    grown_files = []  # [(path, prev_size)]

    for fpath, cur_size in files_with_sizes.items():
        prev = file_cursor.get(fpath)
        if prev is None:
            new_files.append(fpath)
        elif cur_size > prev:
            grown_files.append((fpath, prev))
        # elif cur_size == prev: unchanged — skip
        # elif cur_size < prev: treat as new (truncated file, very rare)
        else:
            if cur_size < prev:
                new_files.append(fpath)

    skipped = len(files_with_sizes) - len(new_files) - len(grown_files)
    is_incremental = bool(file_cursor)
    progress_cb(
        "reading_projects",
        f"{'Incremental: ' if is_incremental else ''}"
        f"{len(new_files)} new, {len(grown_files)} grown, {skipped} unchanged",
    )

    token_logs = []
    session_plans = []
    session_tasks = {}

    if new_files:
        tl, sp, st = _read_project_files_batched(
            client, new_files, claude_dir, source, progress_cb
        )
        token_logs.extend(tl)
        session_plans.extend(sp)
        session_tasks.update(st)

    if grown_files:
        _read_grown_files_batched(
            client, grown_files, claude_dir, source, progress_cb,
            token_logs, session_plans, session_tasks,
        )

    now = datetime.now(timezone.utc).isoformat()
    new_cursor = {"files": files_with_sizes, "synced_at": now}
    return token_logs, session_plans, session_tasks, new_cursor


def _read_grown_files_batched(client, grown_files, claude_dir, source, progress_cb,
                               token_logs, session_plans, session_tasks):
    """Read grown session files in batches.

    For each grown file: reads full content (one SSH round-trip per batch of 50),
    parses only new token log lines (bytes >= prev_size) but full annotations.
    """
    projects_prefix = f"{claude_dir}/projects/"
    batch_size = 50
    total = len(grown_files)

    for batch_start in range(0, total, batch_size):
        batch = grown_files[batch_start:batch_start + batch_size]
        batch_end = min(batch_start + batch_size, total)
        progress_cb("reading_projects", f"Reading grown files {batch_start + 1}-{batch_end} of {total}")

        parts = []
        for fpath, prev_size in batch:
            safe = fpath.replace("'", "'\\''")
            # Embed prev_size in the marker so the parser knows where new bytes start
            parts.append(
                f"echo '<<<GROWN:{fpath}:{prev_size}>>>';"
                f" cat '{safe}' 2>/dev/null;"
                f" echo '<<<END>>>'"
            )
        raw = _exec(client, "; ".join(parts))

        current_file = None
        current_prev = 0
        current_lines = []

        for line in raw.splitlines():
            if line.startswith("<<<GROWN:") and line.endswith(">>>"):
                if current_file and current_lines:
                    _process_grown_file(
                        current_file, current_prev, current_lines,
                        projects_prefix, source, token_logs, session_plans, session_tasks,
                    )
                inner = line[9:-3]  # strip <<<GROWN: and >>>
                last_colon = inner.rfind(":")
                if last_colon > 0:
                    current_file = inner[:last_colon]
                    try:
                        current_prev = int(inner[last_colon + 1:])
                    except ValueError:
                        current_prev = 0
                else:
                    current_file = inner
                    current_prev = 0
                current_lines = []
            elif line == "<<<END>>>":
                if current_file and current_lines:
                    _process_grown_file(
                        current_file, current_prev, current_lines,
                        projects_prefix, source, token_logs, session_plans, session_tasks,
                    )
                current_file = None
                current_prev = 0
                current_lines = []
            elif current_file is not None:
                current_lines.append(line)

        if current_file and current_lines:
            _process_grown_file(
                current_file, current_prev, current_lines,
                projects_prefix, source, token_logs, session_plans, session_tasks,
            )


def _process_grown_file(file_path, prev_size, lines, projects_prefix, source,
                         token_logs, session_plans, session_tasks):
    """Parse a grown session file.

    Token logs: only lines starting at/after prev_size bytes (no duplicates).
    Annotations: full replay for correct task state.
    """
    rel = file_path
    if projects_prefix in file_path:
        rel = file_path.split(projects_prefix, 1)[1]

    parts = rel.split("/")
    if len(parts) < 2:
        return

    proj_name = parts[0]
    session_id = parts[-1].replace(".jsonl", "")
    project_path = _decode_project_path(proj_name)

    # Identify which lines are "new" (their starting byte offset >= prev_size)
    byte_pos = 0
    new_lines = []
    for line in lines:
        if byte_pos >= prev_size:
            new_lines.append(line)
        byte_pos += len(line.encode("utf-8")) + 1  # +1 for the \n

    token_logs.extend(_parse_token_log_lines(new_lines, project_path, session_id, source=source))

    # Full annotation replay — merge will upsert by sessionId
    sp_entries, tasks = _extract_annotations_from_lines(lines, session_id, source=source)
    session_plans.extend(sp_entries)
    if tasks:
        session_tasks[session_id] = list(tasks.values())


def _sync_plans(client, claude_dir, source, plan_cursor):
    """Fetch plan files that are new or changed since last sync (mtime-based).

    Returns (plans, new_cursor_fragment).
    """
    file_cursor = plan_cursor.get("files", {})

    # Get all plan files with mtimes (mtime TAB path)
    raw = _exec(
        client,
        f"find {claude_dir}/plans -name '*.md' -type f 2>/dev/null"
        f" | while IFS= read -r f; do"
        f"   mtime=$(stat -c '%Y' \"$f\" 2>/dev/null || stat -f '%m' \"$f\" 2>/dev/null || echo 0);"
        f"   printf '%s\\t%s\\n' \"$mtime\" \"$f\";"
        f" done",
    )

    files_with_mtimes = {}  # path → mtime
    for line in raw.splitlines():
        if "\t" in line:
            mtime_str, fpath = line.split("\t", 1)
            try:
                files_with_mtimes[fpath.strip()] = int(mtime_str.strip())
            except ValueError:
                files_with_mtimes[fpath.strip()] = 0

    now = datetime.now(timezone.utc).isoformat()

    if not files_with_mtimes:
        return [], {"files": {}, "synced_at": now}

    # Only fetch files that are new or have a newer mtime than the cursor
    to_fetch = [
        fpath for fpath, mtime in files_with_mtimes.items()
        if fpath not in file_cursor or mtime > file_cursor[fpath]
    ]

    if not to_fetch:
        return [], {"files": files_with_mtimes, "synced_at": now}

    plans = _read_remote_plans_for_paths(client, to_fetch, files_with_mtimes, source)
    return plans, {"files": files_with_mtimes, "synced_at": now}


def _read_remote_plans_for_paths(client, plan_files, files_with_mtimes, source):
    """Read a specific list of remote plan .md files. Returns plan list."""
    parts = []
    for fpath in plan_files:
        safe = fpath.replace("'", "'\\''")
        mtime = files_with_mtimes.get(fpath, 0)
        parts.append(
            f"echo '<<<PLAN:{fpath}>>>';"
            f" echo '{mtime}';"
            f" cat '{safe}' 2>/dev/null;"
            f" echo '<<<END>>>'"
        )
    raw = _exec(client, "; ".join(parts))

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
    """Read all remote ~/.claude/plans/*.md files (full fetch, no cursor)."""
    plans, _ = _sync_plans(client, claude_dir, source, {})
    return plans


# ── Account & model helpers ──────────────────────────────────

def _read_account_and_model(client, home):
    """Read credential info and model setting from an open SSH connection.

    Probes known credential file locations and reads ~/.claude/settings.json.
    Returns dict with: has_credentials, cred_path, org_uuid, credential_blob, model.
    credential_blob must be stripped before storing in aggregators/remote_data.
    """
    claude_dir = f"{home}/.claude"

    # Try each credential candidate
    blob = None
    cred_path = None
    for template in _REMOTE_CRED_CANDIDATES:
        path = template.format(home=home)
        raw = _exec(client, f"cat '{path}' 2>/dev/null")
        if raw.strip():
            try:
                blob = json.loads(raw.strip())
                cred_path = path
                break
            except json.JSONDecodeError:
                continue

    # Read model from settings.json
    settings_raw = _exec(client, f"cat '{claude_dir}/settings.json' 2>/dev/null")
    model = "claude-sonnet-4-6"
    if settings_raw.strip():
        try:
            model = json.loads(settings_raw.strip()).get("model", model) or model
        except Exception:
            pass

    return {
        "has_credentials": blob is not None,
        "cred_path": cred_path,
        "org_uuid": blob.get("organizationUuid", "") if blob else "",
        "credential_blob": blob,
        "model": model,
    }


def get_remote_source_info(server_config):
    """Open a fresh SSH connection and read account + model info.
    Used by the live-read API endpoint when Settings tab is opened.
    Does NOT return credential_blob (security).
    """
    client = None
    try:
        client = _connect(server_config)
        home = _exec(client, "echo $HOME").strip()
        info = _read_account_and_model(client, home)
        info.pop("credential_blob", None)
        return info
    except Exception as e:
        return {"has_credentials": False, "model": "claude-sonnet-4-6", "error": str(e)}
    finally:
        if client:
            client.close()


def capture_remote_credentials(server_config):
    """SSH in and read the credential blob from the remote server.
    Returns {blob, cred_path, org_uuid} or {error}.
    """
    client = None
    try:
        client = _connect(server_config)
        home = _exec(client, "echo $HOME").strip()
        info = _read_account_and_model(client, home)
        if not info.get("has_credentials"):
            return {"error": "No credential file found on remote server"}
        return {
            "blob": info["credential_blob"],
            "cred_path": info["cred_path"],
            "org_uuid": info["org_uuid"],
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        if client:
            client.close()


def write_remote_credentials(server_config, blob, cred_path=None):
    """Write a credential blob to the remote server's credential file."""
    import base64 as _b64
    client = None
    try:
        client = _connect(server_config)
        home = _exec(client, "echo $HOME").strip()

        if not cred_path:
            for template in _REMOTE_CRED_CANDIDATES:
                path = template.format(home=home)
                if _exec(client, f"test -f '{path}' && echo yes || echo no").strip() == "yes":
                    cred_path = path
                    break
            if not cred_path:
                cred_path = _REMOTE_CRED_CANDIDATES[0].format(home=home)

        cred_dir = cred_path.rsplit("/", 1)[0]
        encoded = _b64.b64encode(json.dumps(blob).encode()).decode()
        _exec(client, f"mkdir -p '{cred_dir}' && printf '%s' '{encoded}' | base64 -d > '{cred_path}' && chmod 600 '{cred_path}'")
        return {"success": True, "cred_path": cred_path}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if client:
            client.close()


def write_remote_model(server_config, model):
    """Write model setting to the remote ~/.claude/settings.json."""
    import base64 as _b64
    client = None
    try:
        client = _connect(server_config)
        home = _exec(client, "echo $HOME").strip()
        settings_path = f"{home}/.claude/settings.json"

        raw = _exec(client, f"cat '{settings_path}' 2>/dev/null")
        try:
            settings = json.loads(raw.strip()) if raw.strip() else {}
        except Exception:
            settings = {}

        settings["model"] = model
        encoded = _b64.b64encode(json.dumps(settings, indent=2).encode()).decode()
        _exec(client, f"printf '%s' '{encoded}' | base64 -d > '{settings_path}'")
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if client:
            client.close()


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
