from collections import defaultdict
from datetime import datetime, timedelta, timezone
from backend.parsers import parse_history, parse_project_session_logs, parse_plans, scan_session_annotations
from backend.cost_model import estimate_cost, get_model_display_name
from backend.active_sessions import get_active_sessions
import os
import json
import time
import threading
from pathlib import Path


_cache = {}
_cache_times = {}
CACHE_TTL = 60  # seconds

# Remote data store: keyed by server_id
_remote_data = {}  # {"srv-xxx": {"history": [...], "token_logs": [...], "synced_at": "..."}}

# Background sync state: keyed by server_id
_sync_jobs = {}
_sync_lock = threading.Lock()

# Disk cache directory
_CACHE_DIR = Path(os.path.dirname(os.path.dirname(__file__))) / ".cache" / "sources"


def _get_cached(key, loader):
    now = time.time()
    if key in _cache and (now - _cache_times.get(key, 0)) < CACHE_TTL:
        return _cache[key]
    result = loader()
    _cache[key] = result
    _cache_times[key] = now
    return result


def invalidate_cache():
    _cache.clear()
    _cache_times.clear()


# ── Disk persistence for remote data ─────────────────────────

def _disk_path(server_id):
    return _CACHE_DIR / f"{server_id}.json"


def _save_to_disk(server_id, data):
    """Persist synced remote data to disk."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    clean = {
        "synced_at": data.get("synced_at"),
        "history": [{k: v for k, v in r.items() if k != "_datetime"} for r in data.get("history", [])],
        "token_logs": data.get("token_logs", []),
        "session_plans": data.get("session_plans", []),
        "session_tasks": data.get("session_tasks", {}),
        "plans": data.get("plans", []),
    }
    with open(_disk_path(server_id), "w") as f:
        json.dump(clean, f)


def _load_from_disk(server_id):
    """Load cached remote data from disk. Returns None if not found."""
    path = _disk_path(server_id)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        # Rehydrate _datetime on history records
        for r in data.get("history", []):
            if "timestamp" in r:
                r["_datetime"] = datetime.fromtimestamp(r["timestamp"] / 1000, tz=timezone.utc)
        return data
    except Exception:
        return None


def _delete_from_disk(server_id):
    path = _disk_path(server_id)
    if path.exists():
        path.unlink()


def load_all_cached_sources():
    """Load all previously synced remote data from disk into memory.
    Called once at startup."""
    if not _CACHE_DIR.exists():
        return
    for f in _CACHE_DIR.glob("*.json"):
        server_id = f.stem
        data = _load_from_disk(server_id)
        if data:
            _remote_data[server_id] = data


def store_remote_data(server_id, data):
    """Store synced remote data in memory + persist to disk."""
    _remote_data[server_id] = data
    _save_to_disk(server_id, data)
    invalidate_cache()


def clear_remote_data(server_id):
    _remote_data.pop(server_id, None)
    _delete_from_disk(server_id)
    invalidate_cache()


def start_background_sync(server_id, server_config):
    """Launch a background thread to sync a remote server."""
    from backend.ssh_collector import sync_server

    with _sync_lock:
        # Don't start if already syncing
        if server_id in _sync_jobs and _sync_jobs[server_id]["status"] == "syncing":
            return False

        _sync_jobs[server_id] = {
            "status": "syncing",
            "started_at": time.time(),
            "step": "starting",
            "step_detail": "",
            "error": None,
            "result": None,
        }

    def _on_progress(step, detail=""):
        with _sync_lock:
            if server_id in _sync_jobs:
                _sync_jobs[server_id]["step"] = step
                _sync_jobs[server_id]["step_detail"] = detail

    def _do_sync():
        try:
            result = sync_server(server_config, progress_cb=_on_progress)
            with _sync_lock:
                if result.get("success"):
                    store_remote_data(server_id, {
                        "history": result["history"],
                        "token_logs": result["token_logs"],
                        "session_plans": result.get("session_plans", []),
                        "session_tasks": result.get("session_tasks", {}),
                        "plans": result.get("plans", []),
                        "synced_at": result["synced_at"],
                    })
                    _sync_jobs[server_id] = {
                        "status": "done",
                        "started_at": _sync_jobs[server_id]["started_at"],
                        "finished_at": time.time(),
                        "error": None,
                        "result": {
                            "history_count": result["history_count"],
                            "token_log_count": result["token_log_count"],
                            "synced_at": result["synced_at"],
                        },
                    }
                else:
                    _sync_jobs[server_id] = {
                        "status": "error",
                        "started_at": _sync_jobs[server_id]["started_at"],
                        "finished_at": time.time(),
                        "error": result.get("error", "Unknown error"),
                        "result": None,
                    }
        except Exception as e:
            with _sync_lock:
                _sync_jobs[server_id] = {
                    "status": "error",
                    "started_at": _sync_jobs.get(server_id, {}).get("started_at", 0),
                    "finished_at": time.time(),
                    "error": str(e),
                    "result": None,
                }

    t = threading.Thread(target=_do_sync, daemon=True)
    t.start()
    return True


def get_sync_job(server_id):
    """Get background sync status for a specific server."""
    with _sync_lock:
        job = _sync_jobs.get(server_id)
        if not job:
            return None
        elapsed = time.time() - job["started_at"] if job["status"] == "syncing" else 0
        return {**job, "elapsed_seconds": round(elapsed, 1)}


def get_all_sync_jobs():
    """Get all background sync job statuses."""
    with _sync_lock:
        result = {}
        for srv_id, job in _sync_jobs.items():
            elapsed = time.time() - job["started_at"] if job["status"] == "syncing" else 0
            entry = {**job, "elapsed_seconds": round(elapsed, 1)}
            result[srv_id] = entry
        return result


def get_sync_status():
    """Get sync status for all remote servers."""
    result = {}
    for srv_id, data in _remote_data.items():
        result[srv_id] = {
            "synced_at": data.get("synced_at"),
            "history_count": len(data.get("history", [])),
            "token_log_count": len(data.get("token_logs", [])),
        }
    return result


# ── Data accessors (merge local + remote, filter by source) ──

def _history(source=None):
    local = _get_cached("history", parse_history)
    all_records = list(local)

    for srv_id, data in _remote_data.items():
        all_records.extend(data.get("history", []))

    if source and source != "all":
        all_records = [r for r in all_records if r.get("_source") == source]

    all_records.sort(key=lambda r: r.get("timestamp", 0))
    return all_records


def _token_logs(source=None):
    local = _get_cached("token_logs", parse_project_session_logs)
    all_logs = list(local)

    for srv_id, data in _remote_data.items():
        all_logs.extend(data.get("token_logs", []))

    if source and source != "all":
        all_logs = [t for t in all_logs if t.get("_source") == source]

    return all_logs


# ── Aggregators (all accept source filter) ───────────────────

def overview(days=0, source=None):
    records = _history(source)
    if not records:
        return {
            "totalSessions": 0, "totalMessages": 0,
            "dateRange": {"first": None, "last": None},
            "activeSessions": 0, "todayMessages": 0,
            "yesterdayMessages": 0, "estimatedCostUSD": 0,
        }

    cutoff = None
    if days > 0:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)

    filtered = [r for r in records if cutoff is None or r["_datetime"].date() >= cutoff]
    if not filtered:
        filtered = records

    session_ids = set(r.get("sessionId") for r in filtered)
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)

    today_msgs = sum(1 for r in filtered if r["_datetime"].date() == today)
    yesterday_msgs = sum(1 for r in filtered if r["_datetime"].date() == yesterday)

    active = get_active_sessions(source=source)

    # All token logs (unfiltered) for model enrichment; filtered subset for cost.
    all_token_logs = _token_logs(source=None)
    if source and source != "all":
        token_logs = [t for t in all_token_logs if t.get("_source") == source]
    else:
        token_logs = all_token_logs

    # Enrich every active session (local + SSH) with its most recently used model.
    active_by_sid = {s["sessionId"]: s for s in active}
    if active_by_sid:
        latest_models = {}  # {sessionId: (timestamp, model)}
        for t in all_token_logs:
            sid = t.get("sessionId", "")
            if sid not in active_by_sid:
                continue
            model = t.get("model", "")
            if not model:
                continue
            ts = t.get("timestamp", "")
            prev = latest_models.get(sid)
            if prev is None or ts > prev[0]:
                latest_models[sid] = (ts, model)
        for sid, (_, model) in latest_models.items():
            active_by_sid[sid]["model"] = model
    if cutoff:
        token_logs_filtered = [
            t for t in token_logs
            if t.get("timestamp", "")[:10] >= cutoff.isoformat()
        ]
    else:
        token_logs_filtered = token_logs
    total_cost = sum(estimate_cost(t["model"], t) for t in token_logs_filtered)

    return {
        "totalSessions": len(session_ids),
        "totalMessages": len(filtered),
        "dateRange": {
            "first": filtered[0]["_datetime"].isoformat(),
            "last": filtered[-1]["_datetime"].isoformat(),
        },
        "activeSessions": len(active),
        "activeSessionDetails": active,
        "todayMessages": today_msgs,
        "yesterdayMessages": yesterday_msgs,
        "estimatedCostUSD": round(total_cost, 2),
    }


def daily_activity(days=90, source=None):
    records = _history(source)
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)

    by_date = defaultdict(lambda: {"messages": 0, "sessions": set()})
    for r in records:
        d = r["_datetime"].date()
        if days > 0 and d < cutoff:
            continue
        key = d.isoformat()
        by_date[key]["messages"] += 1
        by_date[key]["sessions"].add(r.get("sessionId"))

    return [
        {"date": k, "messageCount": v["messages"], "sessionCount": len(v["sessions"])}
        for k in sorted(by_date.keys())
        for v in [by_date[k]]
    ]


def weekly_activity(source=None):
    records = _history(source)
    by_week = defaultdict(lambda: {"messages": 0, "sessions": set()})
    for r in records:
        dt = r["_datetime"]
        week_start = dt.date() - timedelta(days=dt.weekday())
        key = week_start.isoformat()
        by_week[key]["messages"] += 1
        by_week[key]["sessions"].add(r.get("sessionId"))

    return [
        {"week": k, "messageCount": v["messages"], "sessionCount": len(v["sessions"])}
        for k in sorted(by_week.keys())
        for v in [by_week[k]]
    ]


def monthly_activity(source=None):
    records = _history(source)
    by_month = defaultdict(lambda: {"messages": 0, "sessions": set()})
    for r in records:
        dt = r["_datetime"]
        key = f"{dt.year}-{dt.month:02d}"
        by_month[key]["messages"] += 1
        by_month[key]["sessions"].add(r.get("sessionId"))

    return [
        {"month": k, "messageCount": v["messages"], "sessionCount": len(v["sessions"])}
        for k in sorted(by_month.keys())
        for v in [by_month[k]]
    ]


def project_breakdown(source=None):
    records = _history(source)
    by_project = defaultdict(lambda: {"messages": 0, "sessions": set(), "last": None})
    for r in records:
        project = r.get("project", "unknown")
        by_project[project]["messages"] += 1
        by_project[project]["sessions"].add(r.get("sessionId"))
        dt = r["_datetime"]
        if by_project[project]["last"] is None or dt > by_project[project]["last"]:
            by_project[project]["last"] = dt

    result = [
        {
            "project": project,
            "name": os.path.basename(project) if project != "unknown" else "unknown",
            "messageCount": data["messages"],
            "sessionCount": len(data["sessions"]),
            "lastActive": data["last"].isoformat() if data["last"] else None,
        }
        for project, data in by_project.items()
    ]
    result.sort(key=lambda x: x["messageCount"], reverse=True)
    return result


def hourly_heatmap(source=None):
    records = _history(source)
    grid = [[0] * 24 for _ in range(7)]
    for r in records:
        dt = r["_datetime"]
        local_dt = dt.astimezone()
        grid[local_dt.weekday()][local_dt.hour] += 1

    return {
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "hours": list(range(24)),
        "grid": grid,
    }


def token_summary(source=None):
    token_logs = _token_logs(source)
    by_model = defaultdict(lambda: {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "cost": 0.0, "count": 0,
    })

    for t in token_logs:
        model = t["model"]
        by_model[model]["input_tokens"] += t.get("input_tokens", 0)
        by_model[model]["output_tokens"] += t.get("output_tokens", 0)
        by_model[model]["cache_creation_input_tokens"] += t.get("cache_creation_input_tokens", 0)
        by_model[model]["cache_read_input_tokens"] += t.get("cache_read_input_tokens", 0)
        by_model[model]["cost"] += estimate_cost(model, t)
        by_model[model]["count"] += 1

    result = [
        {
            "model": model,
            "displayName": get_model_display_name(model),
            "inputTokens": data["input_tokens"],
            "outputTokens": data["output_tokens"],
            "cacheCreationTokens": data["cache_creation_input_tokens"],
            "cacheReadTokens": data["cache_read_input_tokens"],
            "totalTokens": (data["input_tokens"] + data["output_tokens"]
                           + data["cache_creation_input_tokens"]
                           + data["cache_read_input_tokens"]),
            "estimatedCostUSD": round(data["cost"], 2),
            "requestCount": data["count"],
        }
        for model, data in by_model.items()
    ]
    result.sort(key=lambda x: x["totalTokens"], reverse=True)
    return result


def daily_token_cost(source=None):
    token_logs = _token_logs(source)
    by_date = defaultdict(lambda: {"cost": 0.0, "tokens": 0})

    for t in token_logs:
        ts = t.get("timestamp", "")
        if not ts:
            continue
        try:
            date_str = ts[:10]
            by_date[date_str]["cost"] += estimate_cost(t["model"], t)
            by_date[date_str]["tokens"] += (
                t.get("input_tokens", 0) + t.get("output_tokens", 0)
            )
        except Exception:
            continue

    return [
        {"date": k, "estimatedCostUSD": round(v["cost"], 4), "totalTokens": v["tokens"]}
        for k in sorted(by_date.keys())
        for v in [by_date[k]]
    ]


def _annotations():
    """Cached local scan merged with remote annotation data."""
    local_plans, local_tasks = _get_cached("session_annotations", scan_session_annotations)

    all_plans = list(local_plans)
    all_tasks = dict(local_tasks)

    for data in _remote_data.values():
        all_plans.extend(data.get("session_plans", []))
        for sid, tasks in data.get("session_tasks", {}).items():
            all_tasks[sid] = tasks

    return all_plans, all_tasks


def sessions_list(source=None):
    records = _history(source)
    by_session = defaultdict(lambda: {
        "messages": 0, "first": None, "last": None, "projects": set(), "source": "local"
    })
    for r in records:
        sid = r.get("sessionId", "unknown")
        by_session[sid]["messages"] += 1
        by_session[sid]["source"] = r.get("_source", "local")
        dt = r["_datetime"]
        if by_session[sid]["first"] is None or dt < by_session[sid]["first"]:
            by_session[sid]["first"] = dt
        if by_session[sid]["last"] is None or dt > by_session[sid]["last"]:
            by_session[sid]["last"] = dt
        by_session[sid]["projects"].add(r.get("project", "unknown"))

    active_pids = {s["sessionId"] for s in get_active_sessions(source=source)}

    # Enrich with plan and task annotations (uses cached single-pass scan)
    session_plans_raw, session_tasks_raw = _annotations()
    # Keep latest plan per session (ExitPlanMode may be called multiple times)
    plan_by_session = {}
    for sp in session_plans_raw:
        sid = sp["sessionId"]
        if sid not in plan_by_session or sp["timestamp"] > plan_by_session[sid][0]:
            plan_by_session[sid] = (sp["timestamp"], sp["slug"])
    plan_by_session = {sid: v[1] for sid, v in plan_by_session.items()}

    result = []
    for sid, data in by_session.items():
        duration_ms = 0
        if data["first"] and data["last"]:
            duration_ms = int((data["last"] - data["first"]).total_seconds() * 1000)
        tasks = session_tasks_raw.get(sid, [])
        result.append({
            "sessionId": sid,
            "messageCount": data["messages"],
            "startTime": data["first"].isoformat() if data["first"] else None,
            "endTime": data["last"].isoformat() if data["last"] else None,
            "durationMs": duration_ms,
            "projects": list(data["projects"]),
            "projectName": os.path.basename(list(data["projects"])[0]) if data["projects"] else "unknown",
            "isActive": sid in active_pids,
            "source": data["source"],
            "planSlug": plan_by_session.get(sid),
            "taskCount": len(tasks),
            "completedTaskCount": sum(1 for t in tasks if t.get("status") == "completed"),
        })
    result.sort(key=lambda x: x["startTime"] or "", reverse=True)
    return result


def plans_list():
    """Return all plans (local + remote) merged with their session binding."""
    # Merge local plan files with remote plan files keyed by slug
    plan_files = {p["slug"]: p for p in _get_cached("plans", parse_plans)}
    for data in _remote_data.values():
        for p in data.get("plans", []):
            slug = p["slug"]
            if slug not in plan_files:
                plan_files[slug] = p

    session_plans_raw, _ = _annotations()

    # Deduplicate: one entry per slug, keeping the latest occurrence
    by_slug = {}
    for sp in session_plans_raw:
        slug = sp["slug"]
        if slug not in by_slug or sp["timestamp"] > by_slug[slug]["timestamp"]:
            by_slug[slug] = sp

    result = []
    for slug, sp in by_slug.items():
        pf = plan_files.get(slug, {})
        result.append({
            "slug": slug,
            "title": pf.get("title", slug),
            "preview": pf.get("preview", ""),
            "content": pf.get("content", ""),
            "sessionId": sp["sessionId"],
            "createdAt": sp["timestamp"] or pf.get("createdAt", ""),
            "allowedPrompts": sp["allowedPrompts"],
            "hasFile": slug in plan_files,
        })

    # Plans on disk that weren't found in any session scan
    for slug, pf in plan_files.items():
        if slug not in by_slug:
            result.append({
                "slug": slug,
                "title": pf["title"],
                "preview": pf["preview"],
                "content": pf["content"],
                "sessionId": None,
                "createdAt": pf["createdAt"],
                "allowedPrompts": [],
                "hasFile": True,
            })

    result.sort(key=lambda x: x["createdAt"] or "", reverse=True)
    return result


def session_tasks(session_id):
    """Return the task list for a specific session."""
    _, session_tasks_raw = _annotations()
    return session_tasks_raw.get(session_id, [])
