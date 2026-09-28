"""Local account metadata. Credentials are never returned, copied or sent here."""

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def parse_date(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def claims_from_token(token):
    try:
        part = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError, AttributeError, IndexError):
        return {}


def codex_identity(home, saved=None):
    """Project only identity and dated subscription claims from the local ID token.

    These are cached login metadata, not a fresh billing API verification.
    Never mistake JWT exp or reset-credit expiry for subscription validity.
    """
    saved = saved or {}
    data = read_json(Path(home) / "auth.json")
    tokens = (data.get("tokens") or {}) if isinstance(data, dict) else {}
    if not isinstance(tokens, dict):
        tokens = {}
    claims = claims_from_token(tokens.get("id_token", ""))
    auth = claims.get("https://api.openai.com/auth") or {}
    if not isinstance(auth, dict):
        auth = {}
    email = saved.get("email") or claims.get("email")
    if not email:
        return None
    account_id = (saved.get("workspaceAccountID") or tokens.get("account_id")
                  or auth.get("chatgpt_account_id") or saved.get("providerAccountID"))
    item = {
        "email": email,
        "account_id": account_id,
        "plan": auth.get("chatgpt_plan_type", ""),
        "subscription": None,
    }
    until = auth.get("chatgpt_subscription_active_until")
    identity_matches = (email == claims.get("email")
                        and account_id == auth.get("chatgpt_account_id"))
    if identity_matches and parse_date(until):
        item["subscription"] = {
            "until": until, "kind": "period", "source": "вход Codex",
            "checked_at": auth.get("chatgpt_subscription_last_checked"),
        }
    return item


def codexbar_config_path():
    if os.environ.get("CODEXBAR_CONFIG"):
        return Path(os.environ["CODEXBAR_CONFIG"]).expanduser()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    legacy = Path.home() / ".codexbar/config.json"
    return legacy if legacy.exists() else xdg / "codexbar/config.json"


def discover_codex():
    """Union system, CodexBar and Orca identities; add only missing profile homes.

    Deduplication needs both email and provider workspace ID, never email alone.
    The returned supplemental paths go into a temporary CodexBar config.
    """
    support = Path.home() / "Library/Application Support"
    groups, supplemental = {}, []

    def add(home, source, saved=None, supplement=False):
        home = Path(home).expanduser()
        item = codex_identity(home, saved)
        if item is None:
            return
        key = (item["email"].casefold(), item["account_id"] or str(home))
        if key not in groups:
            groups[key] = dict(item, sources=[])
            if supplement:
                supplemental.append(str(home))
        group = groups[key]
        if source not in group["sources"]:
            group["sources"].append(source)
        newer, older = item["subscription"], group["subscription"]
        def checked(sub):
            return (parse_date((sub or {}).get("checked_at"))
                    or datetime.min.replace(tzinfo=timezone.utc))
        if newer and (not older or checked(newer) > checked(older)):
            group["subscription"] = newer
        if item["plan"]:
            group["plan"] = item["plan"]

    add(os.environ.get("CODEX_HOME") or Path.home() / ".codex", "System")
    saved = read_json(support / "CodexBar/managed-codex-accounts.json")
    for account in saved.get("accounts", []) if isinstance(saved, dict) else []:
        if account.get("managedHomePath"):
            add(account["managedHomePath"], "CodexBar", account)
    config = read_json(codexbar_config_path())
    for provider in config.get("providers", []) if isinstance(config, dict) else []:
        if provider.get("id") == "codex":
            for home in provider.get("codexProfileHomePaths") or []:
                add(home, "CodexBar", supplement=True)
    for auth in sorted((support / "orca/codex-accounts").glob("*/home/auth.json")):
        add(auth.parent, "Orca", supplement=True)
    return list(groups.values()), supplemental


def claude_plans():
    paths = sorted((Path.home() / ".claude-swap-backup/configs").glob("*.json"))
    paths.append(Path.home() / ".claude.json")
    result = {}
    for path in paths:
        data = read_json(path)
        profile = (data.get("oauthAccount") or {}) if isinstance(data, dict) else {}
        email = profile.get("emailAddress")
        tier = profile.get("organizationRateLimitTier") or ""
        plan = {"claude_max": "Max", "claude_pro": "Pro"}.get(profile.get("organizationType"), "")
        for multiplier in ("20x", "5x"):
            if tier.endswith("_" + multiplier):
                plan += " " + multiplier
        if email and plan:
            result[email.casefold()] = plan
    return result


def subscription_path():
    return Path(os.environ.get("LIMITS_SUBSCRIPTIONS", "~/.config/limits/subscriptions.json")).expanduser()


def load_subscriptions():
    path = subscription_path()
    if not path.exists():
        return {}
    # Unlike optional discovery, do not silently overwrite a corrupt user file.
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("subscriptions.json должен содержать объект")
    for entry in data.values():
        if (not isinstance(entry, dict) or not parse_date(entry.get("until"))
                or entry.get("kind") not in ("period", "renews")):
            raise ValueError("неверная дата или тип в subscriptions.json")
    return data


def save_subscription(provider, email, until, renews=False, source="вручную"):
    if provider not in ("claude", "codex") or "@" not in email or not parse_date(until):
        raise ValueError("ожидается: claude|codex email YYYY-MM-DD")
    data = load_subscriptions()
    data[provider + ":" + email.casefold()] = {
        "until": until, "kind": "renews" if renews else "period", "source": source,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = subscription_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        tmp = Path(stream.name)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
