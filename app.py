import threading
import signal
import os
import re
import shutil
import subprocess
import sys
import json
import logging
import urllib.request
import urllib.error
from pathlib import Path
from flask import Flask, jsonify, request, render_template

# Log to ~/Library/Logs/ on macOS, home dir elsewhere
_log_dir = Path.home() / "Library" / "Logs" if sys.platform == "darwin" else Path.home()
_log_path = _log_dir / "ClaudeUsageMonitor.log"
logging.basicConfig(
    filename=str(_log_path),
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("urllib3").setLevel(logging.WARNING)


def _resource(relative_path):
    """Resolve path whether running as script or PyInstaller bundle."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)
from backend import aggregators
from backend.active_sessions import get_active_sessions
from backend.auth import get_api_key, get_auth_status, save_manual_key, delete_manual_key
from backend.claude_web import (
    migrate_single_to_accounts, list_accounts, get_account,
    save_account, delete_account, fetch_bootstrap, fetch_full_account_usage,
)
from backend.ssh_collector import (
    list_servers, get_server, save_server, delete_server,
    test_connection, sync_server,
)

app = Flask(__name__,
            template_folder=_resource("templates"),
            static_folder=_resource("static"))

_CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"

# Aliases the Claude CLI accepts in addition to full model IDs. Listing them
# alongside the observed model IDs lets users pick "always latest" too.
_MODEL_ALIASES = [
    {"id": "opus", "name": "Opus (latest)"},
    {"id": "sonnet", "name": "Sonnet (latest)"},
    {"id": "haiku", "name": "Haiku (latest)"},
]

# Cache for parsed effort levels (parsed once from `claude --help`).
_effort_cache = None


def _parse_efforts_from_cli():
    """Parse the `--effort <level>` choices straight from the local Claude CLI's
    help output so the list always matches what this machine's binary accepts."""
    global _effort_cache
    if _effort_cache is not None:
        return _effort_cache

    binary = shutil.which("claude")
    levels = []
    if binary:
        try:
            out = subprocess.run(
                [binary, "--help"], capture_output=True, text=True, timeout=5
            ).stdout
            m = re.search(r"--effort\s+<level>\s+[^\(]*\(([^)]*)\)", out)
            if m:
                levels = [
                    x.strip().lower()
                    for x in re.split(r"[,\s]+", m.group(1))
                    if x.strip() and x.strip().lower() != "level"
                ]
        except Exception:
            pass

    if not levels:
        levels = ["low", "medium", "high", "xhigh", "max"]

    _effort_cache = [{"id": lvl, "name": lvl.capitalize()} for lvl in levels]
    return _effort_cache


def _available_models():
    """Build the model list from token logs (machine-observed) plus CLI aliases.
    De-duplicated; observed models listed first by most-recent use."""
    seen_ids = set()
    out = []
    try:
        for m in aggregators.available_models():
            if m["id"] in seen_ids:
                continue
            seen_ids.add(m["id"])
            out.append({"id": m["id"], "name": m["name"]})
    except Exception:
        logging.exception("available_models from token logs failed")

    for a in _MODEL_ALIASES:
        if a["id"] not in seen_ids:
            out.append(a)
            seen_ids.add(a["id"])
    return out


def _read_settings():
    if not _CLAUDE_SETTINGS.exists():
        return {}
    try:
        with open(_CLAUDE_SETTINGS) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_settings(settings):
    _CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    with open(_CLAUDE_SETTINGS, "w") as f:
        json.dump(settings, f, indent=2)

# Auto-migrate legacy single-account config
migrate_single_to_accounts()

# Load previously synced remote data from disk
aggregators.load_all_cached_sources()


@app.route("/")
def index():
    return render_template("index.html")


def _source():
    """Get source filter from query string."""
    return request.args.get("source", None)


# ── Overview ─────────────────────────────────────────────────
@app.route("/api/overview")
def api_overview():
    days = request.args.get("days", 0, type=int)
    return jsonify(aggregators.overview(days, source=_source()))


# ── Activity ─────────────────────────────────────────────────
@app.route("/api/activity/daily")
def api_daily():
    days = request.args.get("days", 90, type=int)
    return jsonify(aggregators.daily_activity(days, source=_source()))


@app.route("/api/activity/weekly")
def api_weekly():
    return jsonify(aggregators.weekly_activity(source=_source()))


@app.route("/api/activity/monthly")
def api_monthly():
    return jsonify(aggregators.monthly_activity(source=_source()))


# ── Projects ─────────────────────────────────────────────────
@app.route("/api/projects")
def api_projects():
    days = int(request.args.get("days", 0))
    return jsonify(aggregators.project_breakdown(days=days, source=_source()))


# ── Sessions ─────────────────────────────────────────────────
@app.route("/api/sessions")
def api_sessions():
    return jsonify(aggregators.sessions_list(source=_source()))


@app.route("/api/sessions/active")
def api_active_sessions():
    return jsonify(get_active_sessions(source=_source()))


@app.route("/api/sessions/kill", methods=["POST"])
def api_kill_session():
    """Terminate a Claude session by PID — works for local and remote."""
    data = request.get_json()
    pid = data.get("pid")
    source = data.get("source", "local")
    if not pid:
        return jsonify({"error": "pid required"}), 400

    if source == "local":
        # Local kill
        active = get_active_sessions(source="local")
        active_pids = {s["pid"] for s in active}
        if pid not in active_pids:
            return jsonify({"error": "PID is not an active local Claude session"}), 404
        try:
            os.kill(pid, signal.SIGTERM)
            return jsonify({"success": True, "pid": pid})
        except ProcessLookupError:
            return jsonify({"error": "Process not found"}), 404
        except PermissionError:
            return jsonify({"error": "Permission denied"}), 403
    elif source.startswith("ssh:"):
        # Remote kill via SSH
        server_id = source.replace("ssh:", "")
        srv = get_server(server_id)
        if not srv:
            return jsonify({"error": "Server not found"}), 404
        client = None
        try:
            from backend.ssh_collector import _connect, _exec
            client = _connect(srv)
            _exec(client, f"kill {pid}")
            return jsonify({"success": True, "pid": pid, "source": source})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            if client:
                client.close()
    else:
        return jsonify({"error": "Unknown source"}), 400


# ── Tokens ───────────────────────────────────────────────────
@app.route("/api/tokens")
def api_tokens():
    days = int(request.args.get("days", 0))
    return jsonify(aggregators.token_summary(days=days, source=_source()))


@app.route("/api/tokens/daily")
def api_daily_tokens():
    days = int(request.args.get("days", 0))
    return jsonify(aggregators.daily_token_cost(days=days, source=_source()))


# ── Heatmap ──────────────────────────────────────────────────
@app.route("/api/heatmap")
def api_heatmap():
    return jsonify(aggregators.hourly_heatmap(source=_source()))


# ── Accounts (multi claude.ai) ──────────────────────────────
@app.route("/api/accounts", methods=["GET"])
def api_list_accounts():
    return jsonify(list_accounts())


@app.route("/api/accounts", methods=["POST"])
def api_create_account():
    data = request.get_json()
    name = data.get("name", "").strip()
    session_key = data.get("session_key", "").strip()
    if not session_key:
        return jsonify({"error": "session_key required"}), 400

    # Validate by calling bootstrap
    try:
        bootstrap = fetch_bootstrap(session_key)
        if "error" in bootstrap:
            return jsonify({"error": f"Invalid session: {bootstrap['error']}"}), 401

        # Auto-name from account if not provided
        if not name:
            orgs = bootstrap.get("organizations", [])
            org_name = orgs[0]["name"] if orgs else ""
            name = f"{bootstrap.get('display_name', '')} - {org_name}".strip(" -")

        orgs = bootstrap.get("organizations", [])
        acc = save_account({
            "name": name,
            "session_key": session_key,
            "org_id": orgs[0]["uuid"] if orgs else "",
            "account_uuid": bootstrap.get("account_uuid", ""),
            "email": bootstrap.get("email", ""),
            "full_name": bootstrap.get("full_name", ""),
            "display_name": bootstrap.get("display_name", ""),
            "org_name": orgs[0]["name"] if orgs else "",
            "org_role": orgs[0]["role"] if orgs else "",
        })
        return jsonify({"success": True, "account": {
            "id": acc["id"], "name": acc["name"],
            "display_name": bootstrap.get("display_name"),
            "email": bootstrap.get("email"),
        }})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/accounts/<account_id>", methods=["PUT"])
def api_update_account(account_id):
    acc = get_account(account_id)
    if not acc:
        return jsonify({"error": "Account not found"}), 404
    data = request.get_json()
    if "name" in data:
        acc["name"] = data["name"]
    if "session_key" in data and data["session_key"].strip():
        acc["session_key"] = data["session_key"].strip()
    save_account(acc)
    return jsonify({"success": True})


@app.route("/api/accounts/<account_id>", methods=["DELETE"])
def api_delete_account(account_id):
    delete_account(account_id)
    return jsonify({"success": True})


@app.route("/api/accounts/<account_id>/usage")
def api_account_usage(account_id):
    try:
        result = fetch_full_account_usage(account_id)
        if "error" in result:
            logging.error("account usage error for %s: %s", account_id, result)
            return jsonify(result), 401
        return jsonify(result)
    except Exception as e:
        logging.exception("account usage exception for %s", account_id)
        return jsonify({"error": str(e)}), 500


# ── Sources (SSH servers) ────────────────────────────────────
@app.route("/api/sources", methods=["GET"])
def api_list_sources():
    servers = list_servers()
    sync_status = aggregators.get_sync_status()
    result = []
    for srv in servers:
        status = sync_status.get(srv["id"], {})
        result.append({
            **srv,
            "synced_at": status.get("synced_at"),
            "history_count": status.get("history_count", 0),
            "token_log_count": status.get("token_log_count", 0),
        })
    return jsonify(result)


@app.route("/api/sources", methods=["POST"])
def api_create_source():
    data = request.get_json()
    srv = save_server({
        "name": data.get("name", ""),
        "host": data.get("host", ""),
        "user": data.get("user", "root"),
        "key_path": data.get("key_path", "~/.ssh/id_rsa"),
        "port": data.get("port", 22),
    })
    return jsonify({"success": True, "server": srv})


@app.route("/api/sources/<server_id>", methods=["DELETE"])
def api_delete_source(server_id):
    delete_server(server_id)
    aggregators.clear_remote_data(server_id)
    return jsonify({"success": True})


@app.route("/api/sources/<server_id>/test", methods=["POST"])
def api_test_source(server_id):
    srv = get_server(server_id)
    if not srv:
        return jsonify({"error": "Server not found"}), 404
    result = test_connection(srv)
    return jsonify(result)


@app.route("/api/sources/<server_id>/sync", methods=["POST"])
def api_sync_source(server_id):
    """Start a background sync for a server.
    Optional body: {"types": ["history"]} to sync only specific data types.
    Valid types: "history", "sessions", "plans". Omit for all three.
    """
    body = request.get_json(silent=True) or {}
    sync_types = body.get("types") or None  # None → all
    srv = get_server(server_id)
    if not srv:
        return jsonify({"error": "Server not found"}), 404
    started = aggregators.start_background_sync(server_id, srv, sync_types=sync_types)
    if not started:
        return jsonify({"status": "already_syncing"})
    return jsonify({"status": "started"})


@app.route("/api/sources/sync-status")
def api_sync_status():
    """Poll endpoint for background sync progress."""
    return jsonify(aggregators.get_all_sync_jobs())


# ── Plans & Tasks ───────────────────────────────────────────
@app.route("/api/plans")
def api_plans():
    return jsonify(aggregators.plans_list())


@app.route("/api/sessions/<session_id>/tasks")
def api_session_tasks(session_id):
    return jsonify(aggregators.session_tasks(session_id))


# ── Model Settings ──────────────────────────────────────────
@app.route("/api/settings/model", methods=["GET"])
def api_get_model():
    settings = _read_settings()
    model = settings.get("model") or None
    return jsonify({"model": model, "available": _available_models()})


@app.route("/api/settings/model", methods=["POST"])
def api_set_model():
    data = request.get_json()
    model = (data.get("model") or "").strip()
    settings = _read_settings()

    if not model:
        settings.pop("model", None)
        _write_settings(settings)
        return jsonify({"success": True, "model": None})

    settings["model"] = model
    _write_settings(settings)
    return jsonify({"success": True, "model": model})


# ── Effort Settings ─────────────────────────────────────────
@app.route("/api/settings/effort", methods=["GET"])
def api_get_effort():
    settings = _read_settings()
    effort = settings.get("effortLevel") or None
    return jsonify({"effort": effort, "available": _parse_efforts_from_cli()})


@app.route("/api/settings/effort", methods=["POST"])
def api_set_effort():
    data = request.get_json()
    effort = (data.get("effort") or "").strip().lower()
    settings = _read_settings()

    if not effort:
        settings.pop("effortLevel", None)
        _write_settings(settings)
        return jsonify({"success": True, "effort": None})

    valid_ids = {e["id"] for e in _parse_efforts_from_cli()}
    if effort not in valid_ids:
        return jsonify({"error": "Invalid effort"}), 400

    settings["effortLevel"] = effort
    _write_settings(settings)
    return jsonify({"success": True, "effort": effort})


# ── Auth (API key / OAuth) ───────────────────────────────────
@app.route("/api/account/status")
def api_account_status():
    return jsonify(get_auth_status())


@app.route("/api/account/key", methods=["POST"])
def api_set_key():
    data = request.get_json()
    save_manual_key(data.get("api_key", "").strip())
    return jsonify({"success": True})


@app.route("/api/account/key", methods=["DELETE"])
def api_delete_key():
    delete_manual_key()
    return jsonify({"success": True})


@app.route("/api/account/usage")
def api_account_api_usage():
    auth = get_api_key()
    key = auth["key"]
    source = auth["source"]
    if not key:
        return jsonify({"error": "No credentials available."}), 401

    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({
                "model": "claude-haiku-4-5-20241022",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]
            }).encode(),
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            headers = dict(resp.headers)
            body = json.loads(resp.read())
        return jsonify({
            "valid": True, "source": source,
            "model_used": body.get("model"),
            "rate_limits": _extract_rate_limits(headers),
            "usage": body.get("usage", {}),
        })
    except urllib.error.HTTPError as e:
        headers = dict(e.headers) if e.headers else {}
        rate_limits = _extract_rate_limits(headers)
        if e.code == 401:
            return jsonify({"valid": False, "source": source, "error": "Invalid or expired credentials"}), 401
        elif e.code == 429:
            return jsonify({"valid": True, "source": source, "rate_limited": True, "rate_limits": rate_limits})
        else:
            error_body = e.read().decode() if e.fp else ""
            try:
                error_data = json.loads(error_body)
            except json.JSONDecodeError:
                error_data = {}
            return jsonify({
                "valid": True, "source": source,
                "error": error_data.get("error", {}).get("message", str(e)),
                "rate_limits": rate_limits,
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _extract_rate_limits(headers):
    keys = [
        "requests-limit", "requests-remaining", "requests-reset",
        "tokens-limit", "tokens-remaining", "tokens-reset",
        "input-tokens-limit", "input-tokens-remaining",
        "output-tokens-limit", "output-tokens-remaining",
    ]
    return {
        k.replace("-", "_"): headers.get(f"anthropic-ratelimit-{k}")
        for k in keys
        if headers.get(f"anthropic-ratelimit-{k}") is not None
    }


if __name__ == "__main__":
    import webview
    from werkzeug.serving import make_server

    # Bind to an OS-assigned ephemeral port so dev and bundled builds never collide.
    server = make_server("127.0.0.1", 0, app, threaded=True)
    port = server.server_port
    logging.info("Flask listening on http://127.0.0.1:%d", port)

    threading.Thread(target=server.serve_forever, daemon=True).start()

    window = webview.create_window(
        "Claude Usage Monitor",
        f"http://localhost:{port}",
        width=1280,
        height=820,
        min_size=(900, 600),
    )
    webview.start()
