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

DEFAULT_ACCOUNT_FALLBACK = "main"
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

# Process-local chat state: the selected account and the installation default
# snapshot. A selection also goes to a tiny per-chat runtime file when the server
# runs under lazy-tool, because lazy-tool may start a fresh pco child per call.
_process_lock = threading.Lock()
_selected_account: str | None = None
_installation_default: tuple[str, str] | None = None

# Stable chat scope: the nearest ancestor lazy-tool process. Cached because the
# process tree does not change for the life of this server process.
_scope_lock = threading.Lock()
_chat_scope_checked = False
_chat_scope_cache: tuple[int, int] | None = None


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


def _proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def _proc_starttime(pid: int) -> int | None:
    """Linux /proc/<pid>/stat field 22, used to distinguish a reused PID."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _proc_ppid(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return None


def _chat_scope() -> tuple[int, int] | None:
    """Return (pid, starttime) of the nearest ancestor `lazy-tool ... serve`.

    lazy-tool may spawn a fresh pco child per tool call, so the pco process is
    not a stable chat identity. Its lazy-tool parent is. None outside lazy-tool
    (direct stdio), where the server process itself is the scope.
    """
    global _chat_scope_checked, _chat_scope_cache
    with _scope_lock:
        if _chat_scope_checked:
            return _chat_scope_cache
        _chat_scope_checked = True
        pid = os.getppid()
        for _ in range(10):
            if pid <= 1:
                break
            cmdline = _proc_cmdline(pid)
            if "lazy-tool" in cmdline and "serve" in cmdline:
                starttime = _proc_starttime(pid)
                if starttime is not None:
                    _chat_scope_cache = (pid, starttime)
                break
            parent = _proc_ppid(pid)
            if parent is None or parent == pid:
                break
            pid = parent
        return _chat_scope_cache


def chat_scope_id() -> str | None:
    """Stable per-chat scope id, or None when running as a direct stdio server."""
    scope = _chat_scope()
    return f"{scope[0]}:{scope[1]}" if scope is not None else None


def _chat_state_path(scope: tuple[int, int]) -> Path:
    """Per-user, per-chat runtime state file. Non-secret; never the config file."""
    runtime = _clean(os.environ.get("XDG_RUNTIME_DIR"))
    if runtime is not None:
        base = Path(runtime) / "pco-mcp"
    else:
        cache = _clean(os.environ.get("XDG_CACHE_HOME"))
        base = (Path(cache) if cache is not None else Path.home() / ".cache") / "pco-mcp"
    return base / f"chat-{scope[0]}-{scope[1]}.json"


def _read_chat_selection(scope: tuple[int, int]) -> str | None:
    try:
        data = json.loads(_chat_state_path(scope).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    account = data.get("account") if isinstance(data, dict) else None
    return _clean(account)


def _write_chat_selection(scope: tuple[int, int], account: str | None) -> None:
    """Atomically set or clear this chat's runtime selection.

    The file holds only an account name (never credentials). A write failure
    raises instead of pretending the chat switched accounts.
    """
    path = _chat_state_path(scope)
    if account is None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise AccountError(
                f"Could not clear this chat's PCO account selection at {path}: {exc}"
            ) from exc
        return

    directory = path.parent
    tmp_name: str | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        fd, tmp_name = tempfile.mkstemp(
            dir=str(directory), prefix=".chat-", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "account": account,
                    "scope_pid": scope[0],
                    "scope_starttime": scope[1],
                },
                handle,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except OSError as exc:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise AccountError(
            f"Could not save this chat's PCO account selection at {path}: {exc}. "
            "Pass account= explicitly on each call instead."
        ) from exc


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
        # so an explicit default such as "main" is still routable.
        return name
    if valid:
        raise _unknown_account(name, valid)
    raise AccountError(
        f"{source} names unknown account '{name}'. {_valid_names_message(valid)}"
    )


def default_account_with_source() -> tuple[str, str]:
    """Resolve the default account name and where it came from (dynamic).

    Sources: ``env:PCO_DEFAULT_ACCOUNT``, ``config``, ``first-key``,
    ``env-credentials``, ``builtin``.
    """
    file_names = list(_load_creds().keys())
    env_available = _env_creds() is not None
    valid = list(file_names)
    if env_available and ENV_ACCOUNT_NAME not in valid:
        valid.append(ENV_ACCOUNT_NAME)

    env_default = _clean(os.environ.get("PCO_DEFAULT_ACCOUNT"))
    if env_default is not None:
        name = _validated_default(env_default, valid, "PCO_DEFAULT_ACCOUNT")
        return name, "env:PCO_DEFAULT_ACCOUNT"

    config_default = _config_default()
    if config_default is not None:
        name = _validated_default(config_default, valid, f"'defaultAccount' in {CONFIG_FILE}")
        return name, "config"

    if file_names:
        return file_names[0], "first-key"
    if env_available:
        return ENV_ACCOUNT_NAME, "env-credentials"
    return DEFAULT_ACCOUNT_FALLBACK, "builtin"


def default_account() -> str:
    """Resolve the default account name."""
    return default_account_with_source()[0]


def installation_default() -> tuple[str, str]:
    """Cached (name, source) installation default, fixed for this process.

    Reads env/config on the first call only; later file or environment changes
    do not alter the snapshot until ``reset_process_state()``.
    """
    global _installation_default
    with _process_lock:
        snapshot = _installation_default
        if snapshot is None:
            snapshot = default_account_with_source()
            _installation_default = snapshot
        return snapshot


def selected_account() -> str | None:
    """Account selected for this chat, or None for the installation default.

    Checks this process first, then the per-chat runtime file. lazy-tool may
    start a fresh pco child per call, so the file carries the selection between
    those children; direct stdio has no chat file and stays in memory.
    """
    with _process_lock:
        local = _selected_account
    if local is not None:
        return local
    scope = _chat_scope()
    if scope is None:
        return None
    return _read_chat_selection(scope)


def select_account(account: str | None) -> str | None:
    """Select the account for this chat.

    ``None`` or a blank string clears the selection. ``"all"`` is rejected.
    A concrete name must resolve and have usable credentials. Under lazy-tool
    the selection is also written atomically to a small per-chat runtime file
    (never the config or credentials file); a write failure raises so the
    caller does not believe a switch happened when it did not.
    """
    global _selected_account
    if account is None or (isinstance(account, str) and not account.strip()):
        scope = _chat_scope()
        if scope is not None:
            _write_chat_selection(scope, None)
        with _process_lock:
            _selected_account = None
        return None
    name = _require_name(account)
    if is_all(name):
        raise AccountError(
            f"Cannot select '{ALL}' as the chat account; pick a concrete account name."
        )
    resolved = resolve_accounts(name)[0]
    credentials(resolved)
    scope = _chat_scope()
    if scope is not None:
        _write_chat_selection(scope, resolved)
    with _process_lock:
        _selected_account = resolved
    return resolved


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


def reset_process_state() -> None:
    """Clear chat selection, scope cache, and installation snapshot; reset caches (tests)."""
    global _selected_account, _installation_default
    global _chat_scope_checked, _chat_scope_cache
    with _process_lock:
        _selected_account = None
        _installation_default = None
    with _scope_lock:
        _chat_scope_checked = False
        _chat_scope_cache = None
    reset_cache()


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
    "chat_scope_id",
    "close_clients",
    "credentials",
    "default_account",
    "default_account_with_source",
    "fan_out",
    "get_client",
    "installation_default",
    "is_all",
    "list_accounts",
    "org_name",
    "reset_cache",
    "reset_process_state",
    "resolve_accounts",
    "select_account",
    "selected_account",
    "set_default_account",
]
