"""Collect Claude usage data from remote servers via SSH."""
import os
import json
import logging
from datetime import datetime, timezone
import paramiko
from backend.parsers import (
    _parse_history_lines, _parse_token_log_lines, _decode_project_path,
    _extract_annotations_from_lines,
)
from backend.auth import _load_config, _save_config, generate_id

log = logging.getLogger(__name__)


# ── Server config CRUD ───────────────────────────────────────

def list_servers():
    config = _load_config()
    servers = config.get("ssh_servers", [])
    # Backfill missing sync_categories on read so existing UI never sees a hole.
    for srv in servers:
        if "sync_categories" not in srv:
            srv["sync_categories"] = list(DEFAULT_SYNC_CATEGORIES)
    return servers


def get_server(server_id):
    for srv in list_servers():
        if srv["id"] == server_id:
            return srv
    return None


SYNC_CATEGORIES = [
    {"id": "history",  "label": "History",  "desc": "Top-level message history (history.jsonl)"},
    {"id": "sessions", "label": "Sessions", "desc": "Per-session token logs + plan/task annotations"},
    {"id": "plans",    "label": "Plans",    "desc": "Plan files under ~/.claude/plans/"},
    {"id": "skills",   "label": "Skills",   "desc": "User, plugin, and project SKILL.md files"},
]
DEFAULT_SYNC_CATEGORIES = [c["id"] for c in SYNC_CATEGORIES]


def save_server(server_dict):
    config = _load_config()
    servers = config.setdefault("ssh_servers", [])
    if "id" not in server_dict:
        server_dict["id"] = generate_id("srv")
    # New servers and any legacy server missing the field default to syncing
    # everything — same behavior as before this preference existed.
    if "sync_categories" not in server_dict:
        server_dict["sync_categories"] = list(DEFAULT_SYNC_CATEGORIES)
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

def sync_server(server_config, progress_cb=None, cursor=None, sync_types=None):
    """Fetch Claude usage data from a remote server.

    cursor: dict loaded via cursor.load_cursor(). Empty dict or None → full sync.
            Non-empty → incremental sync using per-type byte/size/mtime cursors.
    sync_types: set/list of 'history', 'sessions', 'plans'. None means all three.
    Returns dict with success, data, synced_types, and new_cursor (to be saved by caller).
    """
    if cursor is None:
        cursor = {}
    if sync_types is None:
        sync_types = {"history", "sessions", "plans", "skills"}
    else:
        sync_types = set(sync_types)

    server_id = server_config["id"]
    source = f"ssh:{server_id}"
    incremental = bool(cursor)

    def _progress(step, detail=""):
        if progress_cb:
            progress_cb(step, detail)

    # Preserve cursors for unsynced types so they aren't reset
    new_msg_cursor  = cursor.get("messages", {})
    new_sess_cursor = cursor.get("sessions", {})
    new_plan_cursor = cursor.get("plans", {})

    history, history_is_full = [], False
    token_logs, session_plans, session_tasks = [], [], {}
    plans = []
    skills = []
    skills_debug = None

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

        if "history" in sync_types:
            msg_cursor = cursor.get("messages", {})
            mode = "incremental" if msg_cursor else "full"
            _progress("reading_history", f"Reading history.jsonl ({mode})")
            history, new_msg_cursor, history_is_full = _sync_messages(
                client, claude_dir, source, msg_cursor
            )
            _progress("reading_history_done", f"{len(history)} messages")

        if "sessions" in sync_types:
            sess_cursor = cursor.get("sessions", {})
            token_logs, session_plans, session_tasks, new_sess_cursor = _sync_sessions(
                client, claude_dir, source, sess_cursor, _progress
            )

        if "plans" in sync_types:
            plan_cursor = cursor.get("plans", {})
            mode = "incremental" if plan_cursor else "full"
            _progress("reading_plans", f"Reading plan files ({mode})")
            plans, new_plan_cursor = _sync_plans(client, claude_dir, source, plan_cursor)
            _progress("reading_plans_done", f"{len(plans)} plans")

        if "skills" in sync_types:
            try:
                skills, skills_debug = _sync_skills(client, claude_dir, source, _progress)
            except Exception as e:
                log.warning("skills sync exception: %s", e)
                _progress("reading_skills_failed", str(e))
                skills = []
                skills_debug = {"error": str(e)}

        _progress("done", f"{len(history)} msgs, {len(token_logs)} token logs, {len(plans)} plans, {len(skills)} skills")

        return {
            "success": True,
            "server_id": server_id,
            "incremental": incremental,
            "synced_types": list(sync_types),
            "history_is_full": history_is_full,
            "history": history,
            "token_logs": token_logs,
            "session_plans": session_plans,
            "session_tasks": session_tasks,
            "plans": plans,
            "skills": skills,
            "skills_debug": skills_debug,
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

def _extract_org_uuid(blob):
    """Extract organizationUuid from a credential blob.
    Handles both top-level (Keychain format) and nested under claudeAiOauth
    (file-based format used by Claude Code on Linux).
    """
    if not blob:
        return ""
    return (
        blob.get("organizationUuid")
        or blob.get("claudeAiOauth", {}).get("organizationUuid")
        or ""
    )


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
        "org_uuid": _extract_org_uuid(blob),
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


def probe_skills(server_config):
    """One-shot diagnostic: connect, run skill discovery, and return what was
    seen at each stage. Used by the Skills tab when a remote sync returns 0
    skills so the user can tell whether the remote actually has any."""
    client = None
    try:
        client = _connect(server_config)
        home = _exec(client, "echo $HOME").strip()
        claude_dir = f"{home}/.claude"
        diag = {"home": home, "claude_dir": claude_dir}

        # Probe 1: does ~/.claude/skills exist?
        diag["user_skills_dir_exists"] = (
            _exec(client, f"test -d {claude_dir}/skills && echo yes || echo no").strip() == "yes"
        )

        # Probe 2: installed_plugins.json
        plugins_json = _exec(client, f"cat {claude_dir}/plugins/installed_plugins.json 2>/dev/null")
        try:
            import json as _json
            plugins_data = _json.loads(plugins_json) if plugins_json.strip() else {}
        except Exception:
            plugins_data = {}
        plugin_paths = []
        for k, vs in (plugins_data.get("plugins") or {}).items():
            if vs:
                p = vs[-1].get("installPath")
                if p:
                    plugin_paths.append({"plugin": k, "path": p})
        diag["installed_plugin_count"] = len(plugin_paths)
        diag["installed_plugins"] = plugin_paths

        # Probe 3: project cwds
        cwd_raw = _exec(client, (
            f"for d in {claude_dir}/projects/*/; do"
            "   first=$(ls \"$d\"*.jsonl 2>/dev/null | head -1);"
            "   if [ -n \"$first\" ]; then"
            "     grep -m1 -o '\"cwd\":\"[^\"]*\"' \"$first\" 2>/dev/null;"
            "   fi;"
            " done"
        ))
        import re as _re
        cwds = []
        for line in cwd_raw.splitlines():
            m = _re.search(r'"cwd":"([^"]+)"', line)
            if m:
                cwds.append(m.group(1))
        diag["project_cwd_count"] = len(cwds)
        diag["project_cwds_sample"] = cwds[:10]

        # Probe 4: which candidate roots actually exist remotely?
        candidates = [(f"{claude_dir}/skills", "user")]
        for p in plugin_paths:
            candidates.append((p["path"], f"plugin:{p['plugin'].split('@')[0]}"))
            candidates.append((p["path"] + "/skills", f"plugin:{p['plugin'].split('@')[0]}"))
        for cwd in cwds:
            candidates.append((f"{cwd}/.claude/skills", "project"))

        test_parts = []
        for i, (r, _label) in enumerate(candidates):
            safe = r.replace("'", "'\\''")
            test_parts.append(f"if [ -d '{safe}' ]; then echo '{i}'; fi")
        test_out = _exec(client, "; ".join(test_parts)) if test_parts else ""
        existing = []
        for token in test_out.split():
            if token.strip().isdigit():
                idx = int(token)
                if 0 <= idx < len(candidates):
                    existing.append({"path": candidates[idx][0], "label": candidates[idx][1]})
        diag["existing_root_count"] = len(existing)
        diag["existing_roots"] = existing

        # Probe 5: SKILL.md files under those roots
        if existing:
            find_args = " ".join(f"'{r['path']}'".replace(chr(0), "") for r in existing)
            find_out = _exec(
                client,
                f"find {find_args} -maxdepth 3 -name 'SKILL.md' -type f 2>/dev/null",
            )
            files = [p for p in find_out.splitlines() if p.strip().endswith("SKILL.md")]
        else:
            files = []
        diag["skill_md_count"] = len(files)
        diag["skill_md_files_sample"] = files[:20]

        return diag
    except Exception as e:
        return {"error": str(e)}
    finally:
        if client:
            client.close()


def _sync_skills(client, claude_dir, source, progress_cb):
    """Discover and fetch SKILL.md files from the remote machine.

    Mirrors the local discovery in backend/skills.py: user-level (~/.claude/skills/),
    installed-plugin paths from installed_plugins.json, and project-local
    .claude/skills/ for each project enumerated from ~/.claude/projects/.

    Returns a list of skill dicts, each pre-parsed (frontmatter extracted, body
    inlined) so the frontend can serve them without further SSH calls.
    """
    import re as _re
    import json as _json
    from pathlib import PurePosixPath

    debug = {
        "claude_dir": claude_dir,
        "step": "starting",
        "plugin_root_count": 0,
        "project_cwd_count": 0,
        "candidate_count": 0,
        "existing_root_count": 0,
        "skill_file_count": 0,
        "batches": 0,
        "raw_total_bytes": 0,
        "files_seen_in_output": 0,
        "skills_built": 0,
    }
    progress_cb("reading_skills", "Discovering skill roots")

    # ── Round trip 1a: installed_plugins.json ──
    # Use a separate _exec for each phase; combining everything into one
    # shell pipeline produced empty output on macOS zsh remotes (the for-loop
    # never emitted its grep matches when preceded by `cat ...; echo ...;`).
    plugins_json = _exec(
        client, f"cat {claude_dir}/plugins/installed_plugins.json 2>/dev/null"
    )
    plugin_roots = []
    try:
        installed = _json.loads(plugins_json) if plugins_json.strip() else {}
        for plugin_key, versions in (installed.get("plugins") or {}).items():
            if not versions:
                continue
            install_path = versions[-1].get("installPath")
            if not install_path:
                continue
            label = plugin_key.split("@")[0]
            plugin_roots.append((label, install_path))
            plugin_roots.append((label, f"{install_path}/skills"))
    except (_json.JSONDecodeError, ValueError):
        pass
    debug["plugin_root_count"] = len(plugin_roots)

    # ── Round trip 1b: project cwds from ~/.claude/projects/*/<first>.jsonl ──
    cwd_raw = _exec(client, (
        f"for d in {claude_dir}/projects/*/; do"
        "   first=$(ls \"$d\"*.jsonl 2>/dev/null | head -1);"
        "   if [ -n \"$first\" ]; then"
        "     grep -m1 -o '\"cwd\":\"[^\"]*\"' \"$first\" 2>/dev/null;"
        "   fi;"
        " done"
    ))
    project_cwds = []
    for line in cwd_raw.splitlines():
        m = _re.search(r'"cwd":"([^"]+)"', line)
        if m:
            project_cwds.append(m.group(1))
    project_roots = [f"{cwd}/.claude/skills" for cwd in project_cwds]
    debug["project_cwd_count"] = len(project_cwds)

    user_root = f"{claude_dir}/skills"
    candidates = [(user_root, "user", user_root)]
    for label, root in plugin_roots:
        candidates.append((root, f"plugin:{label}", root))
    for cwd, root in zip(project_cwds, project_roots):
        candidates.append((root, "project", cwd))
    debug["candidate_count"] = len(candidates)

    # ── Round trip 2: which candidate dirs actually exist on the remote ──
    test_cmd_parts = [
        f"if [ -d '{r.replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}' ]; then echo '{i}'; fi"
        for i, (r, _, _) in enumerate(candidates)
    ]
    test_out = _exec(client, "; ".join(test_cmd_parts)) if test_cmd_parts else ""
    existing_idxs = {int(x) for x in test_out.split() if x.strip().isdigit()}
    existing = [candidates[i] for i in sorted(existing_idxs)]
    debug["existing_root_count"] = len(existing)
    if not existing:
        debug["step"] = "no_existing_roots"
        log.warning("skills sync diagnostics: %s", debug)
        progress_cb("reading_skills_done", "0 skills (no existing roots)")
        return [], debug

    # ── Round trip 3: find SKILL.md files under existing roots ──
    # No -maxdepth: project skills can live deeper than 3 levels under a project
    # root (e.g. .claude/skills/foo-workspace/iteration-N/SKILL.md), so we accept
    # the slightly wider scan rather than miss them.
    find_args = " ".join(f"'{r}'".replace(chr(0), "") for r, _, _ in existing)
    find_out = _exec(
        client,
        f"find {find_args} -name 'SKILL.md' -type f 2>/dev/null",
    )
    skill_files_raw = [p for p in find_out.splitlines() if p.strip().endswith("SKILL.md")]
    # `find` walks multiple roots independently, so the same SKILL.md can be
    # listed twice (e.g. when both install_path and install_path/skills are
    # passed in). De-duplicate before the round-trip-4 cat.
    seen = set()
    skill_files = []
    for fp in skill_files_raw:
        if fp not in seen:
            seen.add(fp)
            skill_files.append(fp)
    debug["skill_file_count"] = len(skill_files)
    debug["skill_files_sample"] = skill_files[:5]
    if not skill_files:
        debug["step"] = "find_returned_none"
        log.warning("skills sync diagnostics: %s", debug)
        progress_cb("reading_skills_done", "0 skills (find returned none)")
        return [], debug

    # ── Round trip 4: fetch each SKILL.md via its own `cat` exec ──
    # One file per _exec call so the existing 30s channel timeout in _exec
    # acts as a per-file hang guard. SFTP read() had no timeout and hung.
    raw_chunks = []
    for i, fpath in enumerate(skill_files):
        progress_cb(
            "reading_skills",
            f"Fetching {i + 1}/{len(skill_files)}: {fpath.rsplit('/', 1)[-1]}",
        )
        safe = fpath.replace("'", "'\\''")
        # Cap each file at 1 MiB on the remote — a SKILL.md should never need
        # more than a few KB, but a stray symlink / corrupted file should not
        # be allowed to exhaust local memory.
        try:
            body = _exec(client, f"head -c 1048576 '{safe}' 2>/dev/null")
        except Exception as e:
            log.warning("skills sync: head failed for %s: %s", fpath, e)
            body = ""
        raw_chunks.append(f"<<<FILE:{fpath}>>>\n{body}\n<<<END>>>")
        debug["raw_total_bytes"] += len(body)
        debug["batches"] = i + 1  # repurpose as "files fetched so far"
    raw = "\n".join(raw_chunks)

    # ── Parse the batched output back into skill dicts ──
    def _resolve_source(fpath):
        best = None
        for (root, label, scope_root) in existing:
            if fpath.startswith(root.rstrip("/") + "/") or fpath == f"{root.rstrip('/')}/SKILL.md":
                if best is None or len(root) > len(best[0]):
                    best = (root, label, scope_root)
        return best or ("", "unknown", "")

    fm_re = _re.compile(r"^---\s*\n(.*?)\n---\s*\n", _re.DOTALL)

    skills = []
    files_seen = 0
    current_file = None
    current_lines = []

    def _flush(file_path, lines):
        if not file_path:
            return
        text = "\n".join(lines)
        m = fm_re.match(text)
        fm = {}
        body = text
        if m:
            body = text[m.end():]
            for fline in m.group(1).splitlines():
                if ":" not in fline:
                    continue
                k, _, v = fline.partition(":")
                k = k.strip(); v = v.strip()
                if v.startswith('"') and v.endswith('"') and len(v) >= 2:
                    v = v[1:-1]
                fm[k] = v
        _, label, scope_root = _resolve_source(file_path)
        folder = str(PurePosixPath(file_path).parent)
        skill = {
            "name": fm.get("name") or PurePosixPath(folder).name,
            "description": fm.get("description") or "",
            "body": body,
            "source_kind": label,
            "path": file_path,
            "folder": folder,
            "scope_root": scope_root,
            "_source": source,
        }
        if label == "project":
            skill["project"] = scope_root
        skills.append(skill)

    for line in raw.splitlines():
        if line.startswith("<<<FILE:") and line.endswith(">>>"):
            _flush(current_file, current_lines)
            current_file = line[8:-3]
            current_lines = []
            files_seen += 1
        elif line == "<<<END>>>":
            _flush(current_file, current_lines)
            current_file = None
            current_lines = []
        elif current_file is not None:
            current_lines.append(line)
    _flush(current_file, current_lines)

    debug["files_seen_in_output"] = files_seen
    debug["skills_built"] = len(skills)
    debug["step"] = "complete"
    log.warning("skills sync diagnostics: %s", debug)
    progress_cb("reading_skills_done", f"{len(skills)} skills")
    return skills, debug


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
