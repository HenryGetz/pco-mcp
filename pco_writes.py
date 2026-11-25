"""Write transport, validation, and preflight helpers for the PCO MCP.

This module is the only place in the repo allowed to issue an HTTP mutating
request. ``server.py`` and ``pco_accounts.py`` stay read-only by construction.

Safety model:
- Writes require an explicit, concrete account. ``"all"``, a chat selection, or
  the installation default is never used as a write target.
- ``run_write()`` returns a preview and sends nothing unless ``confirm=True``.
- ``confirm=True`` sends exactly one request through ``_send_write``. There are
  no automatic retries: a 429, a 5xx, or a transport failure is reported to the
  caller, never replayed (a POST is never replayed).
- Paths are allowlisted by exact regex; resource IDs are numeric only.
- Preflight helpers use one plain GET, no retries, and fail closed.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import httpx

from pco_accounts import (
    ALL,
    AccountError,
    credentials,
    get_client,
    is_all,
    list_accounts,
    org_name,
)

WRITE_METHODS = frozenset({"POST", "PATCH", "DELETE"})

_JSONAPI_HEADERS = {
    "Content-Type": "application/vnd.api+json",
    "Accept": "application/vnd.api+json",
}
_RESOURCE_TYPES = frozenset({"Plan", "Item", "PlanTime"})
_TIME_TYPES = frozenset({"service", "rehearsal", "other"})
_SERVICE_POSITIONS = frozenset({"pre", "during", "post"})
_MAX_TEAM_REMINDER = 7


class WriteError(RuntimeError):
    """A PCO write that was rejected, failed, or could not be confirmed.

    ``status_code`` is the HTTP status when a response was received.
    ``retryable`` is True for 429 (rate limited); it never means the write was
    retried automatically. ``outcome_unknown`` is True for 5xx and transport
    failures, where the write may or may not have reached PCO.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        retryable: bool = False,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown


# --------------------------------------------------------------------------- #
# Account selection
# --------------------------------------------------------------------------- #


def require_write_account(account: str) -> str:
    """Return a concrete, credential-backed account name for a write.

    Rejects missing/blank names, the ``"all"`` selector, and accounts without
    usable credentials. Never falls back to a selected or default account.
    """
    if not isinstance(account, str) or not account.strip():
        raise WriteError(
            "PCO writes require an explicit concrete account name; pass "
            "account='<name>'. A chat selection or installation default is not used."
        )
    name = account.strip()
    if is_all(name):
        raise WriteError(
            f"Cannot write with account='{ALL}'; pass one concrete account name."
        )
    available = list_accounts()
    if name not in available:
        valid = ", ".join(available) if available else "none"
        raise WriteError(
            f"Account '{name}' is not configured for PCO writes (configured: {valid}). "
            "Pass one of those account names; a write never falls back."
        )
    try:
        credentials(name)
    except AccountError as exc:
        raise WriteError(f"Account '{name}' cannot be used for PCO writes: {exc}") from exc
    return name


def write_context(account: str) -> tuple[str, str]:
    """Validate a write account and return ``(account, org_name)``."""
    name = require_write_account(account)
    try:
        org = org_name(name)
    except AccountError as exc:
        raise WriteError(
            f"Account '{name}' cannot be used for PCO writes: {exc}"
        ) from exc
    return name, org


# --------------------------------------------------------------------------- #
# Value cleaners
# --------------------------------------------------------------------------- #


def require_id(value: Any, field: str) -> str:
    """Return a positive numeric PCO id as a canonical decimal string."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a numeric PCO id, got {value!r}.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not text.isascii() or not text.isdigit():
            raise ValueError(f"{field} must be a numeric PCO id, got {value!r}.")
        number = int(text)
    else:
        raise ValueError(f"{field} must be a numeric PCO id, got {value!r}.")
    if number <= 0:
        raise ValueError(f"{field} must be a positive numeric PCO id, got {value!r}.")
    return str(number)


def clean_text(value: Any, field: str, required: bool = True) -> str | None:
    """Return a stripped non-empty string, or None for an optional empty value."""
    if value is None:
        if required:
            raise ValueError(f"{field} is required.")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string, got {value!r}.")
    text = value.strip()
    if not text:
        if required:
            raise ValueError(f"{field} must be a non-empty string.")
        return None
    return text


def clean_bool(value: Any, field: str) -> bool:
    """Return a boolean; accepts real booleans and the usual 0/1, true/false text."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1"):
            return True
        if text in ("false", "0"):
            return False
    raise ValueError(f"{field} must be a boolean, got {value!r}.")


def clean_int(
    value: Any,
    field: str,
    minimum: int = 0,
    optional: bool = False,
) -> int | None:
    """Return an integer >= ``minimum``, or None when optional and unset."""
    if value is None:
        if optional:
            return None
        raise ValueError(f"{field} is required.")
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer, got {value!r}.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"-?[0-9]+", text):
            raise ValueError(f"{field} must be an integer, got {value!r}.")
        number = int(text)
    else:
        raise ValueError(f"{field} must be an integer, got {value!r}.")
    if number < minimum:
        raise ValueError(f"{field} must be >= {minimum}, got {number}.")
    return number


def clean_iso(value: Any, field: str, required: bool = False) -> str | None:
    """Validate an ISO-8601 timestamp with an explicit timezone; return it as text."""
    if value is None:
        if required:
            raise ValueError(f"{field} is required.")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 string, got {value!r}.")
    text = value.strip()
    if not text:
        if required:
            raise ValueError(f"{field} must be a non-empty ISO-8601 timestamp.")
        return None
    _parse_instant(text, field)
    return text


def clean_time_type(value: Any) -> str:
    """Return one of ``service``, ``rehearsal``, or ``other``."""
    text = _clean_choice(value, "time_type", _TIME_TYPES)
    return text


def clean_service_position(value: Any) -> str:
    """Return one of ``pre``, ``during``, or ``post``."""
    return _clean_choice(value, "service_position", _SERVICE_POSITIONS)


def clean_team_reminders(value: Any) -> dict[str, int] | None:
    """Normalize ``{team_id: minutes}`` with integer team IDs and 0..7 minutes."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(
            f"team_reminders must be an object of team id -> integer 0..7, got {value!r}."
        )
    reminders: dict[str, int] = {}
    for raw_team_id, raw_minutes in value.items():
        if isinstance(raw_team_id, bool):
            raise ValueError(f"team_reminders team id is not numeric: {raw_team_id!r}.")
        if isinstance(raw_team_id, int):
            team_id = require_id(raw_team_id, "team_reminders team id")
        elif isinstance(raw_team_id, str):
            team_id = require_id(raw_team_id, "team_reminders team id")
        else:
            raise ValueError(f"team_reminders team id is not numeric: {raw_team_id!r}.")
        minutes = clean_int(raw_minutes, f"team_reminders[{team_id}]", minimum=0)
        assert minutes is not None  # required=True
        if minutes > _MAX_TEAM_REMINDER:
            raise ValueError(
                f"team_reminders[{team_id}] must be 0..{_MAX_TEAM_REMINDER}, got {minutes}."
            )
        reminders[team_id] = minutes
    return reminders


def _clean_choice(value: Any, field: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be one of {', '.join(sorted(allowed))}.")
    text = value.strip().lower()
    if text not in allowed:
        raise ValueError(
            f"{field} must be one of {', '.join(sorted(allowed))}, got {value!r}."
        )
    return text


def _parse_instant(text: str, field: str) -> datetime:
    """Parse an ISO-8601 timestamp that carries an explicit timezone."""
    normalized = text
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} is not valid ISO-8601: {text!r}.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"{field} must include an explicit timezone offset (e.g. "
            f"'2026-11-22T09:00:00-05:00'), got {text!r}."
        )
    return parsed


# --------------------------------------------------------------------------- #
# Expected-value guards
# --------------------------------------------------------------------------- #


def check_expected_text(actual: Any, expected: Any, label: str) -> None:
    """Raise unless ``actual`` exactly equals ``expected``; None skips the guard."""
    if expected is None:
        return
    if not isinstance(expected, str):
        raise WriteError(f"{label} guard must be a string, got {expected!r}.")
    if actual != expected:
        raise WriteError(
            f"{label} mismatch: expected {expected!r}, found {actual!r}. "
            "No write was sent."
        )


def check_expected_time(actual: Any, expected: Any, label: str) -> None:
    """Raise unless ``actual`` matches ``expected``.

    Timestamps (ISO-8601 with an explicit timezone) are compared as instants, so
    ``09:00-05:00`` equals ``14:00Z``. Date-only values (PCO ``sort_date``) are
    compared as exact dates, which is what plan deletes require.
    """
    if expected is None:
        return
    if not isinstance(expected, str):
        raise WriteError(f"{label} guard must be an ISO-8601 string, got {expected!r}.")
    expected_text = expected.strip()
    expected_date = _date_only_value(expected_text)
    if expected_date is not None:
        actual_text = actual.strip() if isinstance(actual, str) else None
        actual_date = _date_only_value(actual_text) if actual_text else None
        if actual_date is None or actual_date != expected_date:
            raise WriteError(
                f"{label} mismatch: expected date {expected!r}, found {actual!r}. "
                "No write was sent."
            )
        return

    try:
        expected_instant = _parse_instant(expected_text, label)
    except ValueError as exc:
        raise WriteError(f"{label} guard is invalid: {exc}") from exc
    if not isinstance(actual, str):
        raise WriteError(
            f"{label} mismatch: expected instant {expected!r}, found {actual!r}. "
            "No write was sent."
        )
    try:
        actual_instant = _parse_instant(actual.strip(), label)
    except ValueError as exc:
        raise WriteError(
            f"{label} mismatch: expected instant {expected!r}, found {actual!r}. "
            f"No write was sent. ({exc})"
        ) from exc
    if actual_instant != expected_instant:
        raise WriteError(
            f"{label} mismatch: expected instant {expected!r}, found {actual!r}. "
            "No write was sent."
        )


def _date_only_value(text: str) -> date | None:
    """Return a date for exact ``YYYY-MM-DD`` text, else None."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def check_expected_date(actual: Any, expected: Any, label: str) -> None:
    """Raise unless ``actual`` matches ``expected`` as a calendar date or instant.

    PCO plan ``sort_date`` is normally a date-only string (``YYYY-MM-DD``), so a
    date-only guard compares the leading date; a full ISO-8601 guard uses the
    exact-instant comparison from :func:`check_expected_time`.
    """
    if expected is None:
        return
    if not isinstance(expected, str):
        raise WriteError(f"{label} guard must be a date or ISO-8601 string, got {expected!r}.")
    text = expected.strip()
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        try:
            datetime.fromisoformat(text)
        except ValueError as exc:
            raise WriteError(f"{label} guard is invalid: {exc}") from exc
        if not isinstance(actual, str) or actual.strip()[:10] != text:
            raise WriteError(
                f"{label} mismatch: expected date {text!r}, found {actual!r}. "
                "No write was sent."
            )
        return
    check_expected_time(actual, text, label)


# --------------------------------------------------------------------------- #
# Preflight reads (plain GET, no retries)
# --------------------------------------------------------------------------- #


def preflight_object(account: str, path: str) -> dict[str, Any]:
    """GET one PCO object for an explicit account; return its JSON:API data object.

    The returned mapping is the raw resource (``id``, ``type``, ``attributes``),
    so callers read fields as ``obj["attributes"]["title"]``.
    """
    name = require_write_account(account)
    response = _preflight_get(name, path)
    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001 - any decoding failure is a preflight failure
        raise WriteError(
            f"Preflight GET {path} for account '{name}' ({org_name(name)}) "
            f"returned invalid JSON: {exc}. No write was sent."
        ) from exc
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise WriteError(
            f"Preflight GET {path} for account '{name}' ({org_name(name)}) "
            "did not return a single object. No write was sent."
        )
    return data


def preflight_list(
    account: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """GET a PCO collection for an explicit account; return its JSON:API data rows."""
    name = require_write_account(account)
    response = _preflight_get(name, path, params)
    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001 - any decoding failure is a preflight failure
        raise WriteError(
            f"Preflight GET {path} for account '{name}' ({org_name(name)}) "
            f"returned invalid JSON: {exc}. No write was sent."
        ) from exc
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise WriteError(
            f"Preflight GET {path} for account '{name}' ({org_name(name)}) "
            "did not return a collection. No write was sent."
        )
    for row in data:
        if not isinstance(row, dict):
            raise WriteError(
                f"Preflight GET {path} for account '{name}' ({org_name(name)}) "
                f"returned a malformed row: {row!r}. No write was sent."
            )
    return data


def _preflight_get(
    account: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """One plain GET. Fails closed on any non-2xx; never retries."""
    name = require_write_account(account)
    org = org_name(name)
    _check_api_path(path, "preflight")
    client = get_client(name)
    try:
        response = client.get(path, params=params)
    except (httpx.TransportError, TimeoutError) as exc:
        raise WriteError(
            f"Preflight GET {path} failed for account '{name}' ({org}): {exc}. "
            "The read was not retried and no write was sent.",
            retryable=True,
        ) from exc

    status = int(response.status_code)
    if not 200 <= status < 300:
        detail = _error_detail(response)
        suffix = f": {detail}" if detail else ""
        if status == 403:
            message = (
                f"Preflight GET {path} was denied (403) for account '{name}' ({org})"
                f"{suffix}. This account may lack access to that resource. "
                "No write was sent."
            )
        elif status == 404:
            message = (
                f"Preflight target {path} was not found (404) in account "
                f"'{name}' ({org}). No write was sent."
            )
        else:
            message = (
                f"Preflight GET {path} failed for account '{name}' ({org}) with "
                f"HTTP {status}{suffix}. No write was sent."
            )
        raise WriteError(
            message,
            status_code=status,
            retryable=status == 429 or status >= 500,
        )
    return response


def _check_api_path(path: Any, label: str) -> str:
    """Reject absolute URLs and traversal so credentials never leave the PCO host."""
    if not isinstance(path, str):
        raise WriteError(f"{label} path must be a string, got {path!r}.")
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "://" in path
        or "?" in path
        or "#" in path
        or ".." in path
        or any(character.isspace() for character in path)
    ):
        raise WriteError(
            f"{label} path {path!r} must be a relative PCO API path like "
            "'/service_types/123/plans'."
        )
    return path


# --------------------------------------------------------------------------- #
# Write allowlist
# --------------------------------------------------------------------------- #

# (allowed methods, exact path regex, resource type for the JSON:API body)
_WRITE_PATH_RULES: tuple[
    tuple[frozenset[str], re.Pattern[str], str], ...
] = (
    (frozenset({"POST"}), re.compile(r"/service_types/([0-9]+)/plans"), "Plan"),
    (
        frozenset({"PATCH", "DELETE"}),
        re.compile(r"/service_types/([0-9]+)/plans/([0-9]+)"),
        "Plan",
    ),
    (
        frozenset({"POST"}),
        re.compile(r"/service_types/([0-9]+)/plans/([0-9]+)/items"),
        "Item",
    ),
    (
        frozenset({"PATCH", "DELETE"}),
        re.compile(r"/service_types/([0-9]+)/plans/([0-9]+)/items/([0-9]+)"),
        "Item",
    ),
    (
        frozenset({"POST"}),
        re.compile(r"/service_types/([0-9]+)/plans/([0-9]+)/plan_times"),
        "PlanTime",
    ),
    (
        frozenset({"PATCH", "DELETE"}),
        re.compile(r"/service_types/([0-9]+)/plan_times/([0-9]+)"),
        "PlanTime",
    ),
)


def _match_write_path(method: str, path: Any) -> tuple[str, str, str | None]:
    """Return ``(path, resource_type, path_resource_id)`` for an allowlisted path."""
    if not isinstance(path, str):
        raise WriteError(f"Write path must be a string, got {path!r}.")
    for methods, pattern, resource_type in _WRITE_PATH_RULES:
        match = pattern.fullmatch(path)
        if match is None:
            continue
        if method not in methods:
            raise WriteError(
                f"Method {method} is not allowed on {path!r}; allowed: "
                f"{', '.join(sorted(methods))}."
            )
        path_id = match.groups()[-1] if method in ("PATCH", "DELETE") else None
        return path, resource_type, path_id
    raise WriteError(
        f"Write path {path!r} is not on the allowlist. This server only writes "
        "plans, plan items, and plan times."
    )


def _validate_resource(
    *,
    method: str,
    expected_type: str,
    path_id: str | None,
    resource_type: Any,
    resource_id: Any,
    attributes: Any,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Validate the JSON:API resource shape and return normalized parts."""
    clean_type: str | None = None
    if resource_type is not None:
        if not isinstance(resource_type, str) or resource_type not in _RESOURCE_TYPES:
            raise WriteError(
                "resource_type must be one of "
                f"{', '.join(sorted(_RESOURCE_TYPES))}, got {resource_type!r}."
            )
        clean_type = resource_type
    if method in ("POST", "PATCH") and clean_type is None:
        raise WriteError(f"resource_type is required for {method} writes.")
    if clean_type is not None and clean_type != expected_type:
        raise WriteError(
            f"resource_type {clean_type!r} does not match the path resource "
            f"({expected_type})."
        )

    clean_id: str | None = None
    if method == "POST" and resource_id is not None:
        raise WriteError("POST writes must not include a resource_id; the server assigns it.")
    if method in ("PATCH", "DELETE"):
        try:
            clean_id = require_id(resource_id, "resource_id")
        except ValueError as exc:
            raise WriteError(f"{method} requires a valid resource_id: {exc}") from exc
        if path_id is not None and int(clean_id) != int(path_id):
            raise WriteError(
                f"resource_id {clean_id!r} does not match the id in the write path "
                f"({path_id!r})."
            )

    clean_attrs: dict[str, Any] | None = None
    if method in ("POST", "PATCH"):
        if not isinstance(attributes, dict):
            raise WriteError(
                f"attributes must be a JSON:API attributes object for {method} writes."
            )
        if not attributes:
            raise WriteError(f"attributes must contain at least one field for {method} writes.")
        clean_attrs = dict(attributes)
    elif attributes not in (None, {}):
        raise WriteError("DELETE writes carry no body; attributes must be None.")

    return clean_type, clean_id, clean_attrs


def _build_payload(
    method: str,
    resource_type: str | None,
    resource_id: str | None,
    attributes: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build the JSON:API request body, or None for DELETE."""
    if method == "DELETE":
        return None
    data: dict[str, Any] = {"type": resource_type}
    if method == "PATCH":
        data["id"] = resource_id
    data["attributes"] = attributes
    return {"data": data}


# --------------------------------------------------------------------------- #
# Transport and result shaping
# --------------------------------------------------------------------------- #


def _send_write(
    *,
    account: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
) -> httpx.Response:
    """Issue exactly one mutating request. No retries, ever."""
    client = get_client(account)
    kwargs: dict[str, Any] = {"headers": dict(_JSONAPI_HEADERS)}
    if payload is not None:
        kwargs["json"] = payload
    try:
        response = client.request(method, path, **kwargs)
    except (httpx.TransportError, TimeoutError) as exc:
        org = org_name(account)
        raise WriteError(
            f"Write {method} {path} failed in transport for account '{account}' "
            f"({org}): {exc}. The request was not retried and may have reached PCO; "
            f"verify the resource in account '{account}' ({org}) before retrying.",
            outcome_unknown=True,
        ) from exc
    return response


def _success_result(
    response: httpx.Response,
    *,
    account: str,
    org: str,
    operation: str,
    method: str,
    path: str,
    resource_type: str | None,
    resource_id: str | None,
) -> dict[str, Any]:
    status = int(response.status_code)
    request_id = (
        _header_value(response, "x-request-id")
        or _header_value(response, "x-pco-api-request-id")
    )
    resource: Any = None
    if status != 204:
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - an empty/invalid success body is still success
            body = None
        if isinstance(body, dict):
            resource = body.get("data")
    deleted = method == "DELETE" or status == 204

    response_id = None
    if isinstance(resource, dict) and resource.get("id") is not None:
        response_id = str(resource["id"])
    effective_id = response_id or resource_id
    if resource_type and effective_id:
        target = f"{resource_type} {effective_id}"
    elif effective_id:
        target = f"resource {effective_id}"
    else:
        target = resource_type or "resource"
    if method == "DELETE":
        verb = f"Deleted {target}"
    elif method == "POST":
        verb = f"Created {target}"
    else:
        verb = f"Updated {target}"

    return {
        "mode": "write",
        "written": True,
        "account": account,
        "org_name": org,
        "account_source": "explicit",
        "operation": operation,
        "method": method,
        "path": path,
        "status_code": status,
        "request_id": request_id,
        "resource": resource,
        "deleted": deleted,
        "message": f"{verb} in account '{account}' ({org}) via {operation}.",
    }


def _raise_write_status(
    response: httpx.Response,
    *,
    account: str,
    org: str,
    method: str,
    path: str,
) -> None:
    status = int(response.status_code)
    detail = _error_detail(response)
    base = (
        f"PCO write {method} {path} failed for account '{account}' ({org}) "
        f"with HTTP {status}"
    )
    if status == 403:
        raise WriteError(
            f"{base}: {detail + ' ' if detail else ''}This account may lack write "
            "permission in Planning Center, or the plan may be locked. "
            "No retry was attempted.",
            status_code=status,
        )
    if status == 429:
        retry_after = _header_value(response, "retry-after")
        hint = f" PCO asked to wait {retry_after} seconds." if retry_after else ""
        raise WriteError(
            f"{base}{': ' + detail if detail else ''}.{hint} The write was not "
            "retried automatically; retry manually once the rate limit clears.",
            status_code=status,
            retryable=True,
        )
    if status >= 500:
        raise WriteError(
            f"{base}{': ' + detail if detail else ''}. The write may or may not "
            f"have been applied; verify the resource in account '{account}' "
            f"({org}) before retrying. No retry was attempted.",
            status_code=status,
            outcome_unknown=True,
        )
    raise WriteError(
        f"{base}{': ' + detail if detail else ''}. No retry was attempted.",
        status_code=status,
    )


def _error_detail(response: httpx.Response) -> str:
    """Best-effort JSON:API error detail, falling back to a truncated body."""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - non-JSON error bodies are common
        body = None
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list):
            parts: list[str] = []
            for error in errors[:3]:
                if isinstance(error, dict):
                    text = error.get("detail") or error.get("title") or error.get("code")
                    if text:
                        parts.append(str(text))
            if parts:
                return "; ".join(parts)
    text = getattr(response, "text", "") or ""
    return str(text).strip()[:400]


def _header_value(response: httpx.Response, name: str) -> str | None:
    """Read a response header case-insensitively (works for httpx and plain dicts)."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    lowered = name.lower()
    try:
        direct = headers.get(name) or headers.get(lowered)
    except Exception:  # noqa: BLE001 - exotic header mappings should not break a write
        direct = None
    if direct:
        return str(direct)
    try:
        for key, value in headers.items():
            if str(key).lower() == lowered and value:
                return str(value)
    except Exception:  # noqa: BLE001 - see above
        pass
    return None


# --------------------------------------------------------------------------- #
# Public write entry point
# --------------------------------------------------------------------------- #


def run_write(
    *,
    account: str,
    operation: str,
    method: str,
    path: str,
    resource_type: str | None,
    resource_id: str | None,
    attributes: dict[str, Any] | None,
    preflight: dict[str, Any] | None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Preview or perform one allowlisted PCO write under an explicit account.

    Everything is validated before any request is sent. With ``confirm=False``
    (the default) the result is a preview and no mutating request is issued.
    With ``confirm=True`` exactly one request goes through ``_send_write``; a
    429/5xx/transport failure raises ``WriteError`` and is never auto-retried.
    """
    name, org = write_context(account)

    try:
        clean_operation = clean_text(operation, "operation")
    except ValueError as exc:
        raise WriteError(str(exc)) from exc
    assert clean_operation is not None  # required=True

    if not isinstance(method, str) or method.strip().upper() not in WRITE_METHODS:
        raise WriteError(
            f"method must be one of {', '.join(sorted(WRITE_METHODS))}, got {method!r}."
        )
    clean_method = method.strip().upper()

    clean_path, expected_type, path_id = _match_write_path(clean_method, path)
    clean_type, clean_id, clean_attrs = _validate_resource(
        method=clean_method,
        expected_type=expected_type,
        path_id=path_id,
        resource_type=resource_type,
        resource_id=resource_id,
        attributes=attributes,
    )

    if not isinstance(confirm, bool):
        raise WriteError(
            f"confirm must be a boolean, got {confirm!r}; pass confirm=True only "
            "after the user approves the write."
        )
    if preflight is not None and not isinstance(preflight, dict):
        raise WriteError("preflight must be a dict summary of the target, or None.")

    payload = _build_payload(clean_method, clean_type, clean_id, clean_attrs)

    if not confirm:
        return {
            "mode": "preview",
            "would_write": False,
            "confirm_required": True,
            "account": name,
            "org_name": org,
            "account_source": "explicit",
            "operation": clean_operation,
            "method": clean_method,
            "path": clean_path,
            "payload": payload,
            "preflight": preflight,
            "message": (
                f"Preview only: {clean_method} {clean_path} was not sent. "
                f"Re-run with confirm=True to write to account '{name}' ({org})."
            ),
        }

    response = _send_write(
        account=name,
        method=clean_method,
        path=clean_path,
        payload=payload,
    )
    status = int(response.status_code)
    if not 200 <= status < 300:
        _raise_write_status(
            response,
            account=name,
            org=org,
            method=clean_method,
            path=clean_path,
        )
    return _success_result(
        response,
        account=name,
        org=org,
        operation=clean_operation,
        method=clean_method,
        path=clean_path,
        resource_type=clean_type,
        resource_id=clean_id,
    )


__all__ = [
    "WRITE_METHODS",
    "WriteError",
    "check_expected_text",
    "check_expected_time",
    "check_expected_date",
    "clean_bool",
    "clean_int",
    "clean_iso",
    "clean_service_position",
    "clean_team_reminders",
    "clean_text",
    "clean_time_type",
    "preflight_list",
    "preflight_object",
    "require_id",
    "require_write_account",
    "run_write",
    "write_context",
]
