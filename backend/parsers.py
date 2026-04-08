import json
import os
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"


# ── Shared line-parsing helpers (used by local + SSH) ────────

def _parse_history_lines(lines, source="local"):
    """Parse history.jsonl lines into records."""
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            record["_datetime"] = datetime.fromtimestamp(
                record["timestamp"] / 1000, tz=timezone.utc
            )
            record["_source"] = source
            records.append(record)
        except (json.JSONDecodeError, KeyError):
            continue
    return records


def _parse_token_log_lines(lines, project_path, session_id, source="local"):
    """Parse project session .jsonl lines for assistant messages with token usage."""
    results = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        if record.get("type") != "assistant":
            continue

        message = record.get("message", {})
        usage = message.get("usage")
        model = message.get("model", "unknown")
        timestamp = record.get("timestamp", "")

        if usage:
            results.append({
                "model": model,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                "timestamp": timestamp,
                "project": project_path,
                "sessionId": session_id,
                "_source": source,
            })
    return results


# ── Local parsers ────────────────────────────────────────────

def parse_history():
    """Read ~/.claude/history.jsonl and return list of message records."""
    history_file = CLAUDE_DIR / "history.jsonl"
    if not history_file.exists():
        return []
    with open(history_file, "r") as f:
        return _parse_history_lines(f, source="local")


def parse_sessions_metadata():
    """Read all ~/.claude/sessions/*.json and return session metadata."""
    sessions_dir = CLAUDE_DIR / "sessions"
    sessions = []
    if not sessions_dir.exists():
        return sessions
    for f in sessions_dir.glob("*.json"):
        try:
            with open(f, "r") as fp:
                data = json.load(fp)
                data["_startedDatetime"] = datetime.fromtimestamp(
                    data["startedAt"] / 1000, tz=timezone.utc
                )
                sessions.append(data)
        except (json.JSONDecodeError, KeyError):
            continue
    return sessions


def _list_project_dirs():
    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.exists():
        return []
    return [d for d in projects_dir.iterdir() if d.is_dir()]


def _decode_project_path(encoded_name):
    if encoded_name.startswith("-"):
        return "/" + encoded_name[1:].replace("-", "/")
    return encoded_name.replace("-", "/")


def parse_project_session_logs():
    """Scan all project session .jsonl files for assistant messages with token usage."""
    results = []
    for project_dir in _list_project_dirs():
        project_path = _decode_project_path(project_dir.name)
        for jsonl_file in project_dir.glob("*.jsonl"):
            session_id = jsonl_file.stem
            try:
                with open(jsonl_file, "r") as f:
                    results.extend(_parse_token_log_lines(f, project_path, session_id, source="local"))
            except Exception:
                continue
    return results


def get_latest_session_models():
    """Return dict of {sessionId: model} based on the most recent assistant message per session."""
    latest = {}  # {sessionId: (timestamp_str, model)}
    for project_dir in _list_project_dirs():
        for jsonl_file in project_dir.glob("*.jsonl"):
            session_id = jsonl_file.stem
            try:
                with open(jsonl_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if record.get("type") != "assistant":
                            continue
                        model = record.get("message", {}).get("model", "")
                        if not model:
                            continue
                        ts = record.get("timestamp", "")
                        prev = latest.get(session_id)
                        if prev is None or ts > prev[0]:
                            latest[session_id] = (ts, model)
            except Exception:
                continue
    return {sid: info[1] for sid, info in latest.items()}
