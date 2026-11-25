"""Multi-account credential, config, client-cache, and fan-out helpers for the PCO MCP.

Accounts come from ``~/.mpco_credentials.json`` (one entry per account) plus an
optional synthetic ``env`` account built from ``PCO_APP_ID`` + ``PCO_SECRET``
(or ``PCO_APP_SECRET``). This module owns every credential read; ``server.py``
only calls into it.
"""

from __future__ import annotations

import atexit
import base64
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

PCO_BASE_URL = "https://api.planningcenteronline.com/services/v2"
CREDS_FILE = Path(os.path.expanduser("~/.mpco_credentials.json"))
CONFIG_FILE = Path(os.path.expanduser("~/.mpco_config.json"))
ENV_ACCOUNT_NAME = "env"
ALL = "all"

DEFAULT_ACCOUNT_FALLBACK = "unw"
_JSON_RETRY_DELAY = 0.05


class AccountError(RuntimeError):
    """Credential or account configuration problem."""


class UnknownAccountError(AccountError):
    """A concrete account name that does not exist in the configuration."""


# Parsed credentials cache, keyed by (mtime_ns, size) of CREDS_FILE.
_creds_lock = threading.Lock()
_creds_cache: tuple[tuple[int, int] | None, dict[str, Any]] | None = None

# One httpx.Client per (account, app_id, secret).
_clients_lock = threading.Lock()
_clients: dict[tuple[str, str, str], httpx.Client] = {}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _clean(value: Any) -> str | None:
    """Return a stripped non-empty string, else None."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _require_name(account: Any) -> str:
    name = _clean(account)
    if name is None:
        raise AccountError("Account name must be a non-empty string.")
    return name


def _env_creds() -> tuple[str, str] | None:
    """Synthetic 'env' account credentials, read live from the environment."""
    app_id = _clean(os.environ.get("PCO_APP_ID"))
    secret = _clean(os.environ.get("PCO_SECRET")) or _clean(os.environ.get("PCO_APP_SECRET"))
    if app_id and secret:
        return app_id, secret
    return None


def _valid_names_message(valid: list[str]) -> str:
    if not valid:
        return (
            f"No PCO accounts are configured. Add an entry to {CREDS_FILE} "
            "or set PCO_APP_ID and PCO_SECRET."
        )
    return f"Valid accounts: {', '.join(valid)}."


def _unknown_account(name: str, valid: list[str]) -> UnknownAccountError:
    return UnknownAccountError(f"Unknown PCO account '{name}'. {_valid_names_message(valid)}")


# --------------------------------------------------------------------------- #
# File reading
# --------------------------------------------------------------------------- #


def _read_creds_json() -> dict[str, Any]:
    path = CREDS_FILE
    text = None
    last_error: Exception | None = None
    # One re-read after a short pause: a concurrent writer may be mid-write.
    for attempt in range(2):
        if attempt:
            time.sleep(_JSON_RETRY_DELAY)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AccountError(f"Cannot read credentials file {path}: {exc}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if not isinstance(data, dict):
            raise AccountError(
                f"Credentials file {path} must contain a JSON object of account entries."
            )
        return data
    raise AccountError(f"Credentials file {path} is not valid JSON: {last_error}") from last_error


def _load_creds() -> dict[str, Any]:
    """Return the parsed credentials-file mapping, cached by (mtime_ns, size)."""
    global _creds_cache
    path = CREDS_FILE
    try:
        stat = path.stat()
    except FileNotFoundError:
        with _creds_lock:
            _creds_cache = (None, {})
        return {}
    except OSError as exc:
        raise AccountError(f"Cannot stat credentials file {path}: {exc}") from exc

    stamp = (stat.st_mtime_ns, stat.st_size)
    with _creds_lock:
        if _creds_cache is not None and _creds_cache[0] == stamp:
            return dict(_creds_cache[1])
        data = _read_creds_json()
        _creds_cache = (stamp, data)
        return dict(data)


def _load_config(*, strict: bool = False) -> dict[str, Any]:
    """Read CONFIG_FILE. Tolerant by default; strict refuses to clobber bad data."""
    path = CONFIG_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        if strict:
            raise AccountError(f"Cannot read config file {path}: {exc}") from exc
        return {}
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        if strict:
            raise AccountError(f"Config file {path} is not valid JSON: {exc}") from exc
        return {}
    if not isinstance(data, dict):
        if strict:
            raise AccountError(f"Config file {path} must contain a JSON object.")
        return {}
    return dict(data)


def _config_value(config: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in config:
            return config[key]
    return None


def _config_default() -> str | None:
    return _clean(_config_value(_load_config(), "defaultAccount", "default_account"))


def _config_org_names() -> dict[str, Any]:
    value = _config_value(_load_config(), "orgNames", "org_names")
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #


def list_accounts() -> list[str]:
    """Configured account names.

    File accounts are authoritative when present. The synthetic ``env`` account is
    listed only for a legacy env-only setup (no credentials-file accounts), so
    ``account="all"`` never double-counts the same org via a stray env pair.
    """
    names = list(_load_creds().keys())
    if not names and _env_creds() is not None:
        names.append(ENV_ACCOUNT_NAME)
    return names


def _validated_default(name: str, valid: list[str], source: str) -> str:
    if is_all(name):
        raise AccountError(f"{source} cannot be '{ALL}'; pick a concrete account name.")
    if name in valid:
        return name
    if not _load_creds() and _env_creds() is not None:
        # Legacy env-only setup: the env credentials serve any requested name,
        # so an explicit default such as "unw" is still routable.
        return name
    if valid:
        raise _unknown_account(name, valid)
    raise AccountError(
        f"{source} names unknown account '{name}'. {_valid_names_message(valid)}"
    )


def default_account() -> str:
    """Resolve the default account name."""
    file_names = list(_load_creds().keys())
    env_available = _env_creds() is not None
    valid = list(file_names)
    if env_available and ENV_ACCOUNT_NAME not in valid:
        valid.append(ENV_ACCOUNT_NAME)

    env_default = _clean(os.environ.get("PCO_DEFAULT_ACCOUNT"))
    if env_default is not None:
        return _validated_default(env_default, valid, "PCO_DEFAULT_ACCOUNT")

    config_default = _config_default()
    if config_default is not None:
        return _validated_default(config_default, valid, f"'defaultAccount' in {CONFIG_FILE}")

    if file_names:
        return file_names[0]
    if env_available:
        return ENV_ACCOUNT_NAME
    return DEFAULT_ACCOUNT_FALLBACK


def is_all(account: str | None) -> bool:
    """True for the 'all' selector, case-insensitively."""
    return isinstance(account, str) and account.strip().lower() == ALL


def resolve_accounts(account: str | None) -> list[str]:
    """Map a tool's ``account`` argument to concrete account names."""
    if account is None or (isinstance(account, str) and not account.strip()):
        return [default_account()]
    name = _require_name(account)

    if name.lower() == ALL:
        names = list_accounts()
        if not names:
            raise AccountError(
                "'account=\"all\"' was requested but no accounts are configured. "
                + _valid_names_message(names)
            )
        return names

    file_creds = _load_creds()
    valid = list(file_creds.keys())
    env_available = _env_creds() is not None
    if env_available and ENV_ACCOUNT_NAME not in valid:
        valid.append(ENV_ACCOUNT_NAME)

    if name in valid:
        return [name]
    if not file_creds and env_available:
        # Legacy env-only setup: env credentials serve any requested name.
        return [name]
    if not valid:
        raise AccountError(
            f"No PCO accounts are configured, so '{name}' cannot be resolved. "
            + _valid_names_message(valid)
        )
    raise _unknown_account(name, valid)


def org_name(account: str) -> str:
    """Human-readable org name: creds entry > config file > account name."""
    name = _require_name(account)
    entry = _load_creds().get(name)
    if isinstance(entry, dict):
        from_creds = _clean(entry.get("orgName")) or _clean(entry.get("org_name"))
        if from_creds is not None:
            return from_creds
    from_config = _clean(_config_org_names().get(name))
    if from_config is not None:
        return from_config
    return name


def account_meta(account: str) -> dict[str, Any]:
    name = _require_name(account)
    try:
        is_default = name == default_account()
    except AccountError:
        # A broken explicit default must not make account listing unusable.
        is_default = False
    return {
        "account": name,
        "org_name": org_name(name),
        "default": is_default,
    }


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def _extract_credentials(entry: Any, account: str) -> tuple[str, str]:
    if not isinstance(entry, dict):
        raise AccountError(
            f"Credentials for account '{account}' in {CREDS_FILE} must be a JSON object "
            'with "appId" and "appSecret".'
        )
    app_id = _clean(entry.get("appId")) or _clean(entry.get("app_id"))
    secret = (
        _clean(entry.get("appSecret"))
        or _clean(entry.get("app_secret"))
        or _clean(entry.get("secret"))
    )
    if not app_id or not secret:
        missing = []
        if not app_id:
            missing.append("appId")
        if not secret:
            missing.append("appSecret")
        raise AccountError(
            f"Account '{account}' in {CREDS_FILE} is missing "
            f"{' and '.join(missing)}. PCO requires both an app ID and a secret."
        )
    return app_id, secret


def credentials(account: str) -> tuple[str, str]:
    """Return (app_id, secret) for a concrete account name."""
    name = _require_name(account)
    file_creds = _load_creds()

    if name in file_creds:
        # A named credentials-file entry always wins.
        return _extract_credentials(file_creds[name], name)

    if name == ENV_ACCOUNT_NAME:
        env = _env_creds()
        if env is not None:
            return env
        raise AccountError(
            "Account 'env' is not configured: set PCO_APP_ID and one of "
            "PCO_SECRET or PCO_APP_SECRET."
        )

    if not file_creds:
        env = _env_creds()
        if env is not None:
            # Legacy env-only setup: env credentials serve any requested name.
            return env
        raise AccountError(
            f"No credentials found for account '{name}'. Add an entry to {CREDS_FILE} "
            "or set PCO_APP_ID and PCO_SECRET."
        )

    raise _unknown_account(name, list_accounts())


# --------------------------------------------------------------------------- #
# HTTP clients
# --------------------------------------------------------------------------- #


def _close_quietly(client: httpx.Client) -> None:
    try:
        client.close()
    except Exception:
        pass


def _build_client(app_id: str, secret: str) -> httpx.Client:
    auth_str = base64.b64encode(f"{app_id}:{secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_str}",
        "Accept": "application/json",
        "User-Agent": "PlanningCenter-MCP/1.0",
    }
    return httpx.Client(base_url=PCO_BASE_URL, headers=headers, timeout=30.0)


def get_client(account: str) -> httpx.Client:
    """Return the cached client for an account. Callers MUST NOT close it."""
    name = _require_name(account)
    app_id, secret = credentials(name)
    key = (name, app_id, secret)
    with _clients_lock:
        existing = _clients.get(key)
        if existing is not None:
            return existing
        for stale_key in [k for k in _clients if k[0] == name]:
            _close_quietly(_clients.pop(stale_key))
        client = _build_client(app_id, secret)
        _clients[key] = client
        return client


def close_clients() -> None:
    """Close every cached client."""
    with _clients_lock:
        clients = [_clients.pop(key) for key in list(_clients)]
    for client in clients:
        _close_quietly(client)


def reset_cache() -> None:
    """Drop cached clients and the parsed-credentials cache (tests)."""
    global _creds_cache
    close_clients()
    with _creds_lock:
        _creds_cache = None


atexit.register(close_clients)


# --------------------------------------------------------------------------- #
# Config writes
# --------------------------------------------------------------------------- #


def set_default_account(account: str) -> str:
    """Persist the default account to CONFIG_FILE. Never touches the creds file."""
    if is_all(account):
        raise AccountError(f"Cannot set '{ALL}' as the default account; pick a concrete name.")
    name = _require_name(account)
    valid = list_accounts()
    env_only = not _load_creds() and _env_creds() is not None
    if name not in valid and not env_only:
        if not valid:
            raise AccountError(
                f"Cannot set default account '{name}': {_valid_names_message(valid)}"
            )
        raise _unknown_account(name, valid)

    config = _load_config(strict=True)
    config.pop("default_account", None)
    config["defaultAccount"] = name

    path = Path(CONFIG_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".mpco_config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return str(path)


# --------------------------------------------------------------------------- #
# Fan-out
# --------------------------------------------------------------------------- #


def _safe_org_name(account: str) -> str:
    try:
        return org_name(account)
    except Exception:
        return account


def fan_out(accounts: list[str], call: Callable[[str], Any]) -> dict[str, Any]:
    """Run ``call(account)`` sequentially for each account; keep partial results."""
    names = [_require_name(name) for name in accounts]
    if not names:
        raise AccountError("fan_out() requires at least one account.")

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for name in names:
        try:
            data = call(name)
        except Exception as exc:  # noqa: BLE001 - one account must not sink the rest
            errors.append(
                {
                    "account": name,
                    "org_name": _safe_org_name(name),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        else:
            results.append(
                {"account": name, "org_name": _safe_org_name(name), "data": data}
            )

    if errors and not results:
        detail = "; ".join(f"{e['account']} ({e['org_name']}): {e['error']}" for e in errors)
        raise RuntimeError(
            f"All {len(names)} PCO account(s) failed: {detail}"
        )

    return {
        "account": ALL,
        "accounts": names,
        "results": results,
        "errors": errors,
    }


__all__ = [
    "ALL",
    "CONFIG_FILE",
    "CREDS_FILE",
    "ENV_ACCOUNT_NAME",
    "PCO_BASE_URL",
    "AccountError",
    "UnknownAccountError",
    "account_meta",
    "close_clients",
    "credentials",
    "default_account",
    "fan_out",
    "get_client",
    "is_all",
    "list_accounts",
    "org_name",
    "reset_cache",
    "resolve_accounts",
    "set_default_account",
]
