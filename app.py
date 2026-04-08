import webbrowser
import threading
import signal
import os
import json
import urllib.request
import urllib.error
from pathlib import Path
from flask import Flask, jsonify, request, render_template
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
    get_remote_source_info, capture_remote_credentials,
    write_remote_credentials, write_remote_model,
)

app = Flask(__name__)
PORT = 5111

_CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
_AVAILABLE_MODELS = [
    {"id": "claude-sonnet-4-6", "name": "Sonnet 4.6"},
    {"id": "claude-opus-4-6", "name": "Opus 4.6"},
    {"id": "claude-haiku-4-5-20251001", "name": "Haiku 4.5"},
]

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
    return jsonify(aggregators.project_breakdown(source=_source()))


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
    return jsonify(aggregators.token_summary(source=_source()))


@app.route("/api/tokens/daily")
def api_daily_tokens():
    return jsonify(aggregators.daily_token_cost(source=_source()))


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
        })
        return jsonify({"success": True, "account": {
            "id": acc["id"], "name": acc["name"],
            "display_name": bootstrap.get("display_name"),
            "email": bootstrap.get("email"),
        }})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/accounts/active")
def api_active_account():
    from backend.auth import get_active_org_uuid
    org_uuid = get_active_org_uuid()
    accounts = list_accounts()
    active_id = next((a["id"] for a in accounts if a.get("org_id") == org_uuid), None)
    return jsonify({"activeAccountId": active_id, "orgUuid": org_uuid})


@app.route("/api/accounts/<account_id>/refresh-identity", methods=["POST"])
def api_refresh_account_identity(account_id):
    """Re-fetch org_id and account_uuid from the bootstrap API.
    Useful for accounts that were added before org_id was being saved,
    so they can be matched to Keychain / SSH credential blobs.
    """
    acc = get_account(account_id)
    if not acc:
        return jsonify({"error": "Account not found"}), 404
    session_key = acc.get("session_key", "")
    if not session_key:
        return jsonify({"error": "Account has no session_key"}), 400
    try:
        bootstrap = fetch_bootstrap(session_key)
        if "error" in bootstrap:
            return jsonify({"error": bootstrap["error"]}), 401
        orgs = bootstrap.get("organizations", [])
        acc["org_id"] = orgs[0]["uuid"] if orgs else acc.get("org_id", "")
        acc["account_uuid"] = bootstrap.get("account_uuid", acc.get("account_uuid", ""))
        save_account(acc)
        return jsonify({"success": True, "org_id": acc["org_id"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/accounts/<account_id>/capture", methods=["POST"])
def api_capture_account(account_id):
    from backend.auth import get_current_credential_blob
    acc = get_account(account_id)
    if not acc:
        return jsonify({"error": "Account not found"}), 404
    blob = get_current_credential_blob()
    if not blob:
        return jsonify({"error": "No Claude Code credentials found in Keychain"}), 404
    acc["credential_blob"] = blob
    acc["org_id"] = blob.get("organizationUuid", acc.get("org_id", ""))
    save_account(acc)
    return jsonify({"success": True})


@app.route("/api/accounts/<account_id>/activate", methods=["POST"])
def api_activate_account(account_id):
    from backend.auth import apply_credential_blob
    acc = get_account(account_id)
    if not acc:
        return jsonify({"error": "Account not found"}), 404
    blob = acc.get("credential_blob")
    if not blob:
        return jsonify({"error": "No captured credentials — use Capture first"}), 400
    if not apply_credential_blob(blob):
        return jsonify({"error": "Failed to write credentials to Keychain"}), 500
    return jsonify({"success": True})


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
    if "linked_source" in data:
        acc["linked_source"] = data["linked_source"]
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
            return jsonify(result), 401
        return jsonify(result)
    except Exception as e:
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
    """Start a background sync for a server."""
    srv = get_server(server_id)
    if not srv:
        return jsonify({"error": "Server not found"}), 404
    started = aggregators.start_background_sync(server_id, srv)
    if not started:
        return jsonify({"status": "already_syncing"})
    return jsonify({"status": "started"})


@app.route("/api/sources/sync-status")
def api_sync_status():
    """Poll endpoint for background sync progress."""
    return jsonify(aggregators.get_all_sync_jobs())


def _match_org_uuid(org_uuid):
    """Find a local account matching an org UUID.
    Checks org_id field AND credential_blob.organizationUuid (accounts added via
    session_key only may not have org_id populated until usage is fetched).
    Returns dict with matched_account_id and matched_account_name.
    """
    if not org_uuid:
        return {"matched_account_id": None, "matched_account_name": None}
    for a in list_accounts():
        full = get_account(a["id"])
        if not full:
            continue
        if full.get("org_id") == org_uuid:
            return {"matched_account_id": a["id"], "matched_account_name": a.get("name", "")}
        blob_org = (full.get("credential_blob") or {}).get("organizationUuid", "")
        if blob_org and blob_org == org_uuid:
            return {"matched_account_id": a["id"], "matched_account_name": a.get("name", "")}
    return {"matched_account_id": None, "matched_account_name": None}


@app.route("/api/sources/<server_id>/info", methods=["GET"])
def api_source_info(server_id):
    """Live read of active account + model for a source.
    For local: reads Keychain + settings.json.
    For SSH: opens SSH connection (may be slow — ~1-2s).
    """
    if server_id == "local":
        from backend.auth import get_active_org_uuid
        org_uuid = get_active_org_uuid() or ""
        model = "claude-sonnet-4-6"
        if _CLAUDE_SETTINGS.exists():
            try:
                with open(_CLAUDE_SETTINGS) as f:
                    model = json.load(f).get("model", model)
            except Exception:
                pass
        return jsonify({
            "has_credentials": bool(org_uuid),
            "org_uuid": org_uuid,
            "model": model,
            **_match_org_uuid(org_uuid),
        })

    srv = get_server(server_id)
    if not srv:
        return jsonify({"error": "Server not found"}), 404

    info = get_remote_source_info(srv)

    return jsonify({
        **info,
        **_match_org_uuid(info.get("org_uuid", "")),
    })


@app.route("/api/sources/<server_id>/account/activate", methods=["POST"])
def api_source_account_activate(server_id):
    """Push an account's credential blob to a source."""
    data = request.get_json() or {}
    account_id = data.get("account_id")
    acc = get_account(account_id)
    if not acc:
        return jsonify({"error": "Account not found"}), 404
    blob = acc.get("credential_blob")
    if not blob:
        return jsonify({"error": "No captured credentials — use Capture first"}), 400

    if server_id == "local":
        from backend.auth import apply_credential_blob
        return jsonify({"success": apply_credential_blob(blob)})

    srv = get_server(server_id)
    if not srv:
        return jsonify({"error": "Server not found"}), 404
    return jsonify(write_remote_credentials(srv, blob))


@app.route("/api/sources/<server_id>/account/capture", methods=["POST"])
def api_source_account_capture(server_id):
    """Capture credentials from a source into a local account (create or update)."""
    data = request.get_json() or {}
    account_id = data.get("account_id")  # None → create new account

    if server_id == "local":
        from backend.auth import get_current_credential_blob
        blob = get_current_credential_blob()
        if not blob:
            return jsonify({"error": "No credentials found in Keychain"}), 404
        org_uuid = blob.get("organizationUuid", "")
        if account_id:
            acc = get_account(account_id)
            if not acc:
                return jsonify({"error": "Account not found"}), 404
        else:
            acc = {"name": "Local Account", "session_key": "", "org_id": org_uuid, "account_uuid": ""}
        acc["credential_blob"] = blob
        if org_uuid:
            acc["org_id"] = org_uuid
        saved = save_account(acc)
        return jsonify({"success": True, "account_id": saved["id"]})

    srv = get_server(server_id)
    if not srv:
        return jsonify({"error": "Server not found"}), 404

    result = capture_remote_credentials(srv)
    if "error" in result:
        return jsonify(result), 404

    blob = result["blob"]
    org_uuid = result["org_uuid"]

    if account_id:
        acc = get_account(account_id)
        if not acc:
            return jsonify({"error": "Account not found"}), 404
        acc["credential_blob"] = blob
        if org_uuid:
            acc["org_id"] = org_uuid
        save_account(acc)
        return jsonify({"success": True, "account_id": account_id})

    # Import as a new account
    srv_name = srv.get("name") or srv["host"]
    new_acc = save_account({
        "name": f"Account from {srv_name}",
        "session_key": "",
        "org_id": org_uuid,
        "account_uuid": "",
        "credential_blob": blob,
        "linked_source": f"ssh:{server_id}",
    })
    return jsonify({
        "success": True,
        "account_id": new_acc["id"],
        "imported": True,
        "name": new_acc["name"],
    })


@app.route("/api/sources/<server_id>/model", methods=["GET"])
def api_get_source_model(server_id):
    if server_id == "local":
        model = "claude-sonnet-4-6"
        if _CLAUDE_SETTINGS.exists():
            try:
                with open(_CLAUDE_SETTINGS) as f:
                    model = json.load(f).get("model", model)
            except Exception:
                pass
        return jsonify({"model": model})
    srv = get_server(server_id)
    if not srv:
        return jsonify({"error": "Server not found"}), 404
    info = get_remote_source_info(srv)
    return jsonify({"model": info.get("model", "claude-sonnet-4-6"), "error": info.get("error")})


@app.route("/api/sources/<server_id>/model", methods=["PUT"])
def api_set_source_model(server_id):
    data = request.get_json() or {}
    model = data.get("model", "")
    if model not in [m["id"] for m in _AVAILABLE_MODELS]:
        return jsonify({"error": "Invalid model"}), 400
    if server_id == "local":
        settings = {}
        if _CLAUDE_SETTINGS.exists():
            try:
                with open(_CLAUDE_SETTINGS) as f:
                    settings = json.load(f)
            except Exception:
                pass
        settings["model"] = model
        with open(_CLAUDE_SETTINGS, "w") as f:
            json.dump(settings, f, indent=2)
        return jsonify({"success": True})
    srv = get_server(server_id)
    if not srv:
        return jsonify({"error": "Server not found"}), 404
    return jsonify(write_remote_model(srv, model))


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
    model = "claude-sonnet-4-6"
    if _CLAUDE_SETTINGS.exists():
        try:
            with open(_CLAUDE_SETTINGS) as f:
                model = json.load(f).get("model", model)
        except Exception:
            pass
    return jsonify({"model": model, "available": _AVAILABLE_MODELS})


@app.route("/api/settings/model", methods=["POST"])
def api_set_model():
    data = request.get_json()
    model = (data.get("model") or "").strip()
    valid_ids = {m["id"] for m in _AVAILABLE_MODELS}
    if not model or model not in valid_ids:
        return jsonify({"error": "Invalid model"}), 400

    settings = {}
    if _CLAUDE_SETTINGS.exists():
        try:
            with open(_CLAUDE_SETTINGS) as f:
                settings = json.load(f)
        except Exception:
            pass

    settings["model"] = model
    with open(_CLAUDE_SETTINGS, "w") as f:
        json.dump(settings, f, indent=2)

    return jsonify({"success": True, "model": model})


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


def open_browser():
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    threading.Timer(1.0, open_browser).start()
    print(f"Claude Usage Monitor running at http://localhost:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=False)
