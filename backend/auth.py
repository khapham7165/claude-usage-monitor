import json
import subprocess
import os
from uuid import uuid4

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".config.json")


def _load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def _save_config(data):
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


def generate_id(prefix="id"):
    """Generate a short unique ID like 'acc-a1b2c3d4'."""
    return f"{prefix}-{uuid4().hex[:8]}"


def get_oauth_token_from_keychain():
    """Read Claude Code OAuth token from macOS Keychain."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        creds = json.loads(result.stdout.strip())
        oauth = creds.get("claudeAiOauth", {})
        return oauth.get("accessToken")
    except Exception:
        return None


def get_api_key():
    """Get the best available API key: manual key first, then OAuth token."""
    config = _load_config()
    manual_key = config.get("api_key", "")
    if manual_key:
        return {"key": manual_key, "source": "manual"}

    oauth_token = get_oauth_token_from_keychain()
    if oauth_token:
        return {"key": oauth_token, "source": "oauth"}

    return {"key": None, "source": None}


def get_auth_status():
    """Get current auth status for the UI."""
    config = _load_config()
    manual_key = config.get("api_key", "")
    oauth_token = get_oauth_token_from_keychain()

    return {
        "hasManualKey": bool(manual_key),
        "maskedManualKey": (manual_key[:10] + "..." + manual_key[-4:]) if manual_key else "",
        "hasOAuthToken": bool(oauth_token),
        "oauthTokenPrefix": (oauth_token[:16] + "...") if oauth_token else "",
        "activeSource": "manual" if manual_key else ("oauth" if oauth_token else None),
    }


def save_manual_key(key):
    config = _load_config()
    config["api_key"] = key
    _save_config(config)


def delete_manual_key():
    config = _load_config()
    config.pop("api_key", None)
    _save_config(config)
