"""Claude.ai web API integration — multi-account support."""
import os
import sys
import logging
import certifi
import cloudscraper
from backend.auth import _load_config, _save_config, generate_id

log = logging.getLogger(__name__)

# In a PyInstaller bundle, certifi.where() points inside the frozen archive.
# We bundle cacert.pem alongside and point SSL env vars at it explicitly.
if hasattr(sys, "_MEIPASS"):
    _cert = os.path.join(sys._MEIPASS, "certifi", "cacert.pem")
else:
    _cert = certifi.where()
os.environ["SSL_CERT_FILE"] = _cert
os.environ["REQUESTS_CA_BUNDLE"] = _cert


def _get_scraper(session_key):
    scraper = cloudscraper.create_scraper()
    scraper.verify = _cert
    scraper.cookies.set("sessionKey", session_key, domain="claude.ai")
    scraper.headers.update({"Accept-Encoding": "identity"})
    return scraper


def _headers():
    return {
        "anthropic-client-platform": "web_claude_ai",
        "content-type": "application/json",
    }


# ── Account CRUD ─────────────────────────────────────────────

def migrate_single_to_accounts():
    """Auto-migrate legacy single session key to accounts array."""
    config = _load_config()
    if "accounts" in config:
        return
    old_key = config.pop("claude_session_key", "")
    if not old_key:
        return
    config["accounts"] = [{
        "id": generate_id("acc"),
        "name": "Default",
        "session_key": old_key,
        "org_id": config.pop("claude_org_id", ""),
        "account_uuid": config.pop("claude_account_uuid", ""),
    }]
    _save_config(config)


def list_accounts():
    """List all accounts (with masked keys for UI)."""
    config = _load_config()
    accounts = config.get("accounts", [])
    result = []
    for acc in accounts:
        key = acc.get("session_key", "")
        result.append({
            "id": acc["id"],
            "name": acc.get("name", ""),
            "email": acc.get("email", ""),
            "full_name": acc.get("full_name", ""),
            "display_name": acc.get("display_name", ""),
            "org_name": acc.get("org_name", ""),
            "org_role": acc.get("org_role", ""),
            "maskedKey": (key[:16] + "..." + key[-4:]) if len(key) > 20 else key[:8] + "...",
            "org_id": acc.get("org_id", ""),
            "account_uuid": acc.get("account_uuid", ""),
            "hasKey": bool(key),
            "linked_source": acc.get("linked_source", ""),
            "hasCredential": bool(acc.get("credential_blob")),
        })
    return result


def get_account(account_id):
    """Get full account dict by ID."""
    config = _load_config()
    for acc in config.get("accounts", []):
        if acc["id"] == account_id:
            return acc
    return None


def save_account(account_dict):
    """Create or update an account. If account_dict has no 'id', generates one."""
    config = _load_config()
    accounts = config.setdefault("accounts", [])

    if "id" not in account_dict:
        account_dict["id"] = generate_id("acc")

    # Update existing or append new
    for i, acc in enumerate(accounts):
        if acc["id"] == account_dict["id"]:
            accounts[i] = account_dict
            _save_config(config)
            return account_dict

    accounts.append(account_dict)
    _save_config(config)
    return account_dict


def delete_account(account_id):
    config = _load_config()
    config["accounts"] = [a for a in config.get("accounts", []) if a["id"] != account_id]
    _save_config(config)


# ── Claude.ai API calls ─────────────────────────────────────

def _api_bootstrap(scraper):
    resp = scraper.get("https://claude.ai/api/bootstrap", headers=_headers())
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}", "status": resp.status_code}
    data = resp.json()
    account = data.get("account", {})
    memberships = account.get("memberships", [])
    orgs = [
        {"uuid": m.get("organization", {}).get("uuid"),
         "name": m.get("organization", {}).get("name"),
         "role": m.get("role")}
        for m in memberships
    ]
    result = {
        "account_uuid": account.get("uuid"),
        "email": account.get("email_address"),
        "full_name": account.get("full_name"),
        "display_name": account.get("display_name"),
        "organizations": orgs,
    }
    log.debug("bootstrap result: %s", result)
    return result


def _api_usage(scraper, org_id):
    resp = scraper.get(
        f"https://claude.ai/api/organizations/{org_id}/usage",
        headers=_headers(),
    )
    if resp.status_code != 200:
        log.error("usage %s: HTTP %s body=%s", org_id, resp.status_code, resp.text[:500])
        return {"error": f"HTTP {resp.status_code}"}
    return resp.json()


def _api_spend_limit(scraper, org_id, account_uuid):
    resp = scraper.get(
        f"https://claude.ai/api/organizations/{org_id}/overage_spend_limit"
        f"?account_uuid={account_uuid}",
        headers=_headers(),
    )
    if resp.status_code != 200:
        return None
    return resp.json() or None


def _api_overage_credit_grant(scraper, org_id):
    resp = scraper.get(
        f"https://claude.ai/api/organizations/{org_id}/overage_credit_grant",
        headers=_headers(),
    )
    if resp.status_code != 200:
        return None
    return resp.json()


def _api_prepaid_credits(scraper, org_id):
    resp = scraper.get(
        f"https://claude.ai/api/organizations/{org_id}/prepaid/credits",
        headers=_headers(),
    )
    if resp.status_code != 200:
        return None
    return resp.json()


def fetch_bootstrap(session_key):
    """Standalone bootstrap fetch (used when adding accounts)."""
    scraper = _get_scraper(session_key)
    try:
        return _api_bootstrap(scraper)
    finally:
        scraper.close()


def fetch_full_account_usage(account_id):
    """Fetch complete usage for an account using a single shared HTTP session."""
    acc = get_account(account_id)
    if not acc:
        return {"error": "Account not found"}

    session_key = acc.get("session_key", "")
    if not session_key:
        return {"error": "No session key"}

    scraper = _get_scraper(session_key)
    try:
        bootstrap = _api_bootstrap(scraper)
        if "error" in bootstrap:
            return bootstrap

        account_uuid = bootstrap["account_uuid"]
        orgs = bootstrap.get("organizations", [])
        if not orgs:
            return {"error": "No organizations found"}

        # Try each org until we find one with accessible usage data.
        # The saved org_id is tried first if still valid, then all others.
        saved_org = acc.get("org_id", "")
        org_order = sorted(orgs, key=lambda o: (0 if o["uuid"] == saved_org else 1))
        org_id = None
        usage = None
        for candidate in org_order:
            u = _api_usage(scraper, candidate["uuid"])
            if "error" not in u:
                org_id = candidate["uuid"]
                usage = u
                break
        if org_id is None:
            return {"error": "No accessible organization found — check that your session key has usage permissions"}

        acc["org_id"] = org_id
        acc["account_uuid"] = account_uuid
        save_account(acc)
        spend_limit = _api_spend_limit(scraper, org_id, account_uuid)
        overage_grant = _api_overage_credit_grant(scraper, org_id)
        prepaid = _api_prepaid_credits(scraper, org_id)
    finally:
        scraper.close()

    # Determine account type and build unified stats
    is_enterprise = bool(spend_limit and spend_limit.get("seat_tier"))
    extra = (usage or {}).get("extra_usage", {})
    five_hour = (usage or {}).get("five_hour")
    seven_day = (usage or {}).get("seven_day")

    stats = {
        "account_id": account_id,
        "account": bootstrap,
        "org_id": org_id,
        "is_enterprise": is_enterprise,
        "raw_usage": usage,
        "raw_spend_limit": spend_limit,
        "raw_overage_grant": overage_grant,
        "raw_prepaid": prepaid,
    }

    if is_enterprise:
        # Enterprise: spend limit is the main metric
        used = spend_limit.get("used_credits", 0)
        limit = spend_limit.get("monthly_credit_limit", 0)
        stats["tier"] = (spend_limit.get("seat_tier") or "").replace("_", " ")
        stats["monthly_spend_usd"] = used / 100
        stats["monthly_limit_usd"] = limit / 100
        stats["monthly_pct"] = round((used / limit * 100), 1) if limit else 0
    else:
        # Personal: extra_usage + prepaid + overage grant
        stats["tier"] = "personal"
        if extra and extra.get("monthly_limit"):
            stats["monthly_spend_usd"] = extra["used_credits"] / 100
            stats["monthly_limit_usd"] = extra["monthly_limit"] / 100
            stats["monthly_pct"] = round(extra.get("utilization", 0), 1)

        if prepaid:
            stats["prepaid_balance_usd"] = prepaid.get("amount", 0) / 100
            stats["prepaid_currency"] = prepaid.get("currency", "USD")

        if overage_grant:
            stats["overage_grant_usd"] = overage_grant.get("amount_minor_units", 0) / 100
            stats["overage_granted"] = overage_grant.get("granted", False)
            stats["overage_eligible"] = overage_grant.get("eligible", False)

    # Rate limits from usage
    if five_hour:
        stats["rate_5h_pct"] = five_hour.get("utilization", 0)
        stats["rate_5h_reset"] = five_hour.get("resets_at")
    if seven_day:
        stats["rate_7d_pct"] = seven_day.get("utilization", 0)
        stats["rate_7d_reset"] = seven_day.get("resets_at")

    return stats
