#!/usr/bin/env python3
"""Planning Center Online (PCO) Services MCP Server for omp.

Integrates with Planning Center Services API (v2) using credentials from
~/.mpco_credentials.json or environment variables.

Read tools take ``account`` and resolve it per call:

1. explicit ``account=`` (a name, or ``"all"``) always wins;
2. this chat's selection (``pco_use_account``), keyed to the stable lazy-tool
   process when running under lazy-tool, else kept in this server process;
3. the installation default snapshot (env/config/first key), cached per process.

``"all"`` fans out over every configured account. Selections never cross chats;
``pco_set_default_account(..., persist=True)`` is the only read-side call that
writes the config file, and it affects new chats only.

Write tools are different: the account must be an explicit, concrete account
name. ``"all"``, omitted accounts, the chat selection, and the installation
default are rejected. Every write is preview-only until ``confirm=True`` and is
sent exactly once (no automatic retries). All mutating HTTP lives in
``pco_writes.py``.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

import httpx
from fastmcp import FastMCP

# The deployed server.py is a symlink outside the repo, so sys.path[0] is the
# deployed directory. Put the real repo directory first to find pco_accounts.py.
_REPO_DIR = os.path.dirname(os.path.realpath(__file__))
if _REPO_DIR not in sys.path:
    sys.path.insert(0, _REPO_DIR)

from pco_accounts import (
    AccountError,
    account_meta,
    chat_scope_id,
    credentials,
    fan_out,
    get_client,
    installation_default,
    is_all,
    list_accounts,
    org_name,
    resolve_accounts,
    select_account,
    selected_account,
    set_default_account,
)
from pco_writes import (
    check_expected_date,
    check_expected_text,
    check_expected_time,
    clean_bool,
    clean_int,
    clean_iso,
    clean_service_position,
    clean_team_reminders,
    clean_text,
    clean_time_type,
    preflight_list,
    preflight_object,
    require_id,
    run_write,
    write_context,
)

mcp = FastMCP("planning-center")

# One value per server process: chats get their own process (and thus their own
# account selection), so this identifies the scope of pco_use_account.
INSTANCE_ID = uuid.uuid4().hex[:12]


def extract_plan_id(plan_id_or_url: str | int) -> str:
    s = str(plan_id_or_url).strip()
    match = re.search(r"(\d{5,})", s)
    if match:
        return match.group(1)
    if s.isdigit():
        return s
    raise ValueError(f"Could not extract numeric plan ID from '{plan_id_or_url}'")


def _tag_account_source(result: Any, source: str) -> Any:
    """Label where the routed account came from: dict result, or every list row."""
    if isinstance(result, dict):
        result["account_source"] = source
    elif isinstance(result, list):
        for row in result:
            if isinstance(row, dict):
                row["account_source"] = source
    return result


def _dispatch(account: Optional[str], call: Callable[[str], Any]) -> Any:
    """Run ``call`` for the routed account, or fan out over every account for "all".

    Precedence: explicit ``account`` argument -> this chat's selection
    (``pco_use_account``) -> the installation default snapshot. The chosen source
    is tagged on the result as ``account_source``.
    """
    if is_all(account):
        envelope = fan_out(resolve_accounts(account), call)
        envelope["account_source"] = "all"
        return envelope

    if account is not None and (not isinstance(account, str) or account.strip()):
        return _tag_account_source(call(resolve_accounts(account)[0]), "explicit")

    selected = selected_account()
    if selected is not None:
        try:
            credentials(selected)
            name = resolve_accounts(selected)[0]
        except AccountError as exc:
            valid = list_accounts()
            raise AccountError(
                f"This chat's selected PCO account '{selected}' is no longer usable: {exc} "
                f"Call pco_use_account() with one of {valid or 'the configured accounts'} "
                "to pick another account, or call pco_use_account() with no argument to "
                "clear the selection and use the installation default."
            ) from exc
        return _tag_account_source(call(name), "chat")

    return _tag_account_source(call(installation_default()[0]), "installation")


def _has_credentials(account: str) -> bool:
    try:
        credentials(account)
    except AccountError:
        return False
    return True


@mcp.tool()
def pco_list_accounts(detailed: bool = False) -> list[str] | list[dict[str, Any]]:
    """List configured Planning Center account names.

    detailed=True adds org_name, has_credentials, and per-row flags: `default`
    (the installation default snapshot this process routes to), `selected`
    (this chat's pco_use_account selection), and `effective` (what omitted
    account= calls actually use: the selection when set, else the snapshot).
    """
    names = list_accounts()
    if not detailed:
        return list(names)

    try:
        snapshot: Optional[str] = installation_default()[0]
    except AccountError:
        # A broken installation default must not make the listing unusable.
        snapshot = None
    selected = selected_account()
    effective = selected if selected is not None else snapshot

    out: list[dict[str, Any]] = []
    for name in names:
        meta = account_meta(name)
        out.append({
            "account": meta["account"],
            "org_name": meta["org_name"],
            "default": name == snapshot,
            "selected": name == selected,
            "effective": name == effective,
            "has_credentials": _has_credentials(name),
        })
    return out


@mcp.tool()
def pco_account_status() -> dict[str, Any]:
    """Report this chat's PCO account routing. No network calls.

    scope_id identifies the stable chat (the lazy-tool process) when running
    under lazy-tool; pid and instance_id identify the current pco process, which
    lazy-tool may replace on every call. installation_default is the process
    snapshot (not a fresh config read), so it matches what omitted account=
    calls route to. The effective account is this chat's selection when set,
    else that snapshot. routing_warning is set when no accounts are configured,
    when several accounts exist with no selection, or when the saved selection
    is no longer usable.
    """
    names = list_accounts()
    selected = selected_account()
    scope_id = chat_scope_id()

    try:
        inst_name, inst_source = installation_default()
    except AccountError as exc:
        if selected is None:
            raise AccountError(
                f"The installation default PCO account cannot be resolved: {exc} "
                "Fix PCO_DEFAULT_ACCOUNT or ~/.mpco_config.json, or call "
                "pco_use_account(<name>) to route this chat to a configured account."
            ) from exc
        inst_name, inst_source = None, None

    effective = selected if selected is not None else inst_name

    warning: Optional[str] = None
    if not names:
        warning = (
            "No PCO accounts are configured. Add an entry to ~/.mpco_credentials.json "
            "or set PCO_APP_ID and PCO_SECRET before calling a PCO tool."
        )
    elif selected is not None:
        if not _has_credentials(selected):
            warning = (
                f"This chat's selected account '{selected}' is no longer configured "
                "or has unusable credentials; tool calls that omit account= will fail "
                "until you call pco_use_account(<name>) or pco_use_account() to clear "
                "the selection."
            )
    elif len(names) > 1:
        warning = (
            f"{len(names)} PCO accounts are configured and this chat has no selection; "
            f"omitted account= calls route to the installation default '{inst_name}'. "
            "Call pco_use_account(<name>) to pin this chat to another org."
        )

    accounts: list[dict[str, Any]] = []
    for name in names:
        meta = account_meta(name)
        accounts.append({
            "account": meta["account"],
            "org_name": meta["org_name"],
            "default": name == inst_name,
            "selected": name == selected,
            "effective": name == effective,
            "has_credentials": _has_credentials(name),
        })

    return {
        "instance_id": INSTANCE_ID,
        "pid": os.getpid(),
        "scope": "chat" if scope_id is not None else "process",
        "scope_id": scope_id,
        "installation_default": inst_name,
        "installation_default_source": inst_source,
        "selected_account": selected,
        "effective_account": effective,
        "effective_source": "chat" if selected is not None else "installation",
        "routing_warning": warning,
        "accounts": accounts,
    }


@mcp.tool()
def pco_use_account(account: Optional[str] = None) -> dict[str, Any]:
    """Select the account for THIS chat only. Never writes config or credentials.

    Under lazy-tool the selection is stored in a small non-secret per-chat
    runtime file keyed to this chat's stable lazy-tool process, because
    lazy-tool may start a fresh pco child per call. It does not affect other
    chats. Omitted (or empty) argument clears the selection, so omitted
    account= calls fall back to the installation default. An explicit account=
    on any tool always wins. Returns pco_account_status() plus changed/message.
    """
    before = selected_account()
    after = select_account(account)
    status = pco_account_status()
    changed = before != after
    if after is None:
        message = (
            "Cleared this chat's account selection; omitted account= calls now route "
            f"to the installation default '{status['effective_account']}'."
        )
    else:
        message = (
            f"This chat now routes omitted account= calls to '{after}' "
            f"({status['effective_source']}). An explicit account= still overrides it."
        )
    return {**status, "changed": changed, "message": message}


@mcp.tool()
def pco_set_default_account(account: str, persist: bool = False) -> dict[str, Any]:
    """Set the account for this chat. No config write unless persist=True.

    The account is validated and selected for THIS chat immediately. Without
    persist, only this chat's non-secret runtime selection changes. persist=True
    also writes ~/.mpco_config.json, which affects only NEW chats: running chats
    keep their installation snapshot and any per-chat selection. Rejects "all".
    """
    name = (account or "").strip()
    if not name:
        raise ValueError("account is required")
    if is_all(name):
        raise ValueError("Cannot set 'all' as the default account")
    resolve_accounts(name)  # validates the name against configured accounts
    credentials(name)  # validates that the account is usable

    config_file: Optional[str] = None
    if persist:
        config_file = set_default_account(name)
    select_account(name)

    return {
        "default_account": name,
        "scope": "persisted" if persist else "chat",
        "config_file": config_file,
        "accounts": list_accounts(),
        "status": pco_account_status(),
    }


def _list_service_types_single(account: str) -> list[dict[str, Any]]:
    client = get_client(account)
    org = org_name(account)
    resp = _get(client, "/service_types", {"per_page": 100})
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return [
        {
            "id": st["id"],
            "name": st["attributes"].get("name"),
            "frequency": st["attributes"].get("frequency"),
            "account": account,
            "org_name": org,
        }
        for st in data
    ]


@mcp.tool()
def pco_list_service_types(account: Optional[str] = None) -> list[dict[str, Any]] | dict[str, Any]:
    """List all service types (e.g. Sunday Morning, Youth, Chapels) per account.

    Account routing: omitted `account` uses this chat's selection
    (`pco_use_account`), else the installation default; an explicit name always
    overrides; `"all"` fans out over every configured org.
    """
    return _dispatch(account, _list_service_types_single)


def _list_plans_single(
    service_type_id: Optional[str],
    filter: str,
    per_page: int,
    account: str,
) -> list[dict[str, Any]]:
    client = get_client(account)
    org = org_name(account)

    st_names: dict[str, Optional[str]] = {}
    if service_type_id:
        st_ids = [str(service_type_id)]
        try:
            st_resp: Optional[httpx.Response] = _get(client, f"/service_types/{st_ids[0]}")
        except httpx.HTTPError:
            st_resp = None
        if st_resp is not None and st_resp.status_code == 200:
            st = st_resp.json().get("data") or {}
            st_names[st_ids[0]] = (st.get("attributes") or {}).get("name")
        else:
            st_names[st_ids[0]] = None
    else:
        sts_resp = _get(client, "/service_types", {"per_page": 100})
        sts = sts_resp.json().get("data", []) if sts_resp.status_code == 200 else []
        st_ids = [st["id"] for st in sts]
        st_names = {st["id"]: (st.get("attributes") or {}).get("name") for st in sts}

    plans_out: list[dict[str, Any]] = []
    for st_id in st_ids:
        params: dict[str, Any] = {"per_page": per_page, "order": "sort_date"}
        if filter in ("future", "past"):
            params["filter"] = filter
        resp = _get(client, f"/service_types/{st_id}/plans", params)
        if resp.status_code != 200:
            continue
        for p in resp.json().get("data", []):
            attrs = p["attributes"]
            plans_out.append({
                "id": p["id"],
                "service_type_id": st_id,
                "service_type_name": st_names.get(st_id),
                "title": attrs.get("title") or attrs.get("dates"),
                "dates": attrs.get("dates"),
                "sort_date": attrs.get("sort_date"),
                "account": account,
                "org_name": org,
            })
    return plans_out


@mcp.tool()
def pco_list_plans(
    service_type_id: Optional[str] = None,
    filter: str = "future",
    per_page: int = 10,
    account: Optional[str] = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """List plans for a service type (or across service types if service_type_id is omitted).

    filter can be 'future', 'past', or 'all'. Account routing: omitted `account`
    uses this chat's selection (`pco_use_account`), else the installation
    default; an explicit name always overrides; `"all"` fans out over every
    configured org.
    """
    return _dispatch(
        account,
        lambda name: _list_plans_single(service_type_id, filter, per_page, name),
    )


def _fetch_plan_times(
    client: httpx.Client,
    service_type_id: str,
    plan_id: str,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """PlanTime rows sorted by starts_at; non-200 becomes an error string, never a failure."""
    path = f"/service_types/{service_type_id}/plans/{plan_id}/plan_times"
    try:
        resp = _get(client, path, {"per_page": 100})
    except httpx.HTTPError as exc:
        return [], str(exc)
    if resp.status_code != 200:
        return [], f"GET {path} failed ({resp.status_code}): {resp.text[:200]}"
    rows = []
    for row in resp.json().get("data", []):
        attrs = row.get("attributes", {})
        rows.append({
            "id": row.get("id"),
            "name": attrs.get("name"),
            "time_type": attrs.get("time_type"),
            "starts_at": attrs.get("starts_at"),
            "ends_at": attrs.get("ends_at"),
        })
    rows.sort(key=lambda t: (t["starts_at"] is None, t["starts_at"] or ""))
    return rows, None


def _get_plan_single(
    plan_id: str,
    account: str,
    include_attachments: bool,
) -> dict[str, Any]:
    client = get_client(account)
    org = org_name(account)

    # Find which service_type contains this plan
    sts_resp = _get(client, "/service_types", {"per_page": 100})
    sts_resp.raise_for_status()
    st_data = sts_resp.json().get("data", [])

    st_id = None
    plan_raw = None
    for st in st_data:
        cand_id = st["id"]
        check_resp = _get(client, f"/service_types/{cand_id}/plans/{plan_id}")
        if check_resp.status_code == 200:
            st_id = cand_id
            plan_raw = check_resp.json().get("data")
            break

    if not st_id or not plan_raw:
        raise RuntimeError(f"Plan {plan_id} not found under account '{account}' ({org})")

    plan_attrs = plan_raw.get("attributes", {})
    plan_title = plan_attrs.get("title") or plan_attrs.get("dates") or f"Plan {plan_id}"

    # Fetch items with included song, arrangement, and key
    items_resp = _get(
        client,
        f"/service_types/{st_id}/plans/{plan_id}/items",
        {"include": "song,arrangement,key,item_notes", "per_page": 100},
    )
    items_resp.raise_for_status()
    raw_items = items_resp.json()

    included_map = {f"{x['type']}:{x['id']}": x for x in raw_items.get("included", [])}

    items_out = []
    for item in raw_items.get("data", []):
        i_attrs = item.get("attributes", {})
        item_type = i_attrs.get("item_type")
        title = i_attrs.get("title") or "Untitled"
        sequence = i_attrs.get("sequence")
        description = i_attrs.get("description")

        item_dict: dict[str, Any] = {
            "id": item.get("id"),
            "sequence": sequence,
            "title": title,
            "type": item_type,
            "description": description,
            "account": account,
            "org_name": org,
        }

        if item_type == "song":
            song_rel = item.get("relationships", {}).get("song", {}).get("data")
            arr_rel = item.get("relationships", {}).get("arrangement", {}).get("data")
            key_rel = item.get("relationships", {}).get("key", {}).get("data")

            song_obj = included_map.get(f"Song:{song_rel['id']}") if song_rel else None
            arr_obj = included_map.get(f"Arrangement:{arr_rel['id']}") if arr_rel else None
            key_obj = included_map.get(f"Key:{key_rel['id']}") if key_rel else None

            song_attrs = song_obj.get("attributes", {}) if song_obj else {}
            arr_attrs = arr_obj.get("attributes", {}) if arr_obj else {}
            key_attrs = key_obj.get("attributes", {}) if key_obj else {}

            key_name = (
                i_attrs.get("key_name")
                or key_attrs.get("name")
                or key_attrs.get("starting_key")
                or arr_attrs.get("default_key")
            )

            item_dict.update({
                "song_id": song_rel.get("id") if song_rel else None,
                "song_title": song_attrs.get("title") or title,
                "author": song_attrs.get("author"),
                "ccli_number": song_attrs.get("ccli_number"),
                "arrangement_id": arr_rel.get("id") if arr_rel else None,
                "arrangement_name": arr_attrs.get("name"),
                "key": key_name,
                "bpm": arr_attrs.get("bpm"),
                "meter": arr_attrs.get("meter"),
                "arrangement_notes": arr_attrs.get("notes"),
            })

            if include_attachments and song_rel and arr_rel:
                att_url = f"/songs/{song_rel['id']}/arrangements/{arr_rel['id']}/attachments"
                att_resp = _get(client, att_url)
                if att_resp.status_code == 200:
                    atts = []
                    for a in att_resp.json().get("data", []):
                        aa = a.get("attributes", {})
                        atts.append({
                            "id": a.get("id"),
                            "filename": aa.get("filename"),
                            "filetype": aa.get("filetype"),
                            "display_name": aa.get("display_name"),
                        })
                    item_dict["attachments"] = atts

        items_out.append(item_dict)

    items_out.sort(key=lambda x: x.get("sequence") or 0)

    plan_times, plan_times_error = _fetch_plan_times(client, st_id, plan_id)

    result: dict[str, Any] = {
        "plan_id": plan_id,
        "title": plan_title,
        "dates": plan_attrs.get("dates"),
        "sort_date": plan_attrs.get("sort_date"),
        "service_type_id": st_id,
        "account": account,
        "org_name": org,
        "counts": {
            "service_time_count": plan_attrs.get("service_time_count"),
            "rehearsal_time_count": plan_attrs.get("rehearsal_time_count"),
            "other_time_count": plan_attrs.get("other_time_count"),
            "plan_people_count": plan_attrs.get("plan_people_count"),
            "items_count": plan_attrs.get("items_count"),
            "total_length": plan_attrs.get("total_length"),
            "multi_day": plan_attrs.get("multi_day"),
        },
        "plan_times": plan_times,
        "items": items_out,
    }
    if plan_times_error:
        result["plan_times_error"] = plan_times_error
    return result


@mcp.tool()
def pco_get_plan(
    plan_id_or_url: str,
    account: Optional[str] = None,
    include_attachments: bool = True,
) -> dict[str, Any]:
    """Retrieve full plan details, setlist order, songs, arrangements, keys, notes, and attachments from a plan ID or URL.

    Account routing: omitted `account` uses this chat's selection
    (`pco_use_account`), else the installation default; an explicit name always
    overrides; `"all"` fans out over every configured org (envelope result with
    account_source="all" and an ambiguous flag).
    """
    plan_id = extract_plan_id(plan_id_or_url)
    if is_all(account):
        envelope = fan_out(
            resolve_accounts(account),
            lambda name: _get_plan_single(plan_id, name, include_attachments),
        )
        envelope["ambiguous"] = len(envelope["results"]) > 1
        envelope["account_source"] = "all"
        return envelope
    return _dispatch(
        account,
        lambda name: _get_plan_single(plan_id, name, include_attachments),
    )


def _search_songs_single(query: str, per_page: int, account: str) -> list[dict[str, Any]]:
    client = get_client(account)
    org = org_name(account)
    resp = _get(client, "/songs", {"where[title]": query, "per_page": per_page})
    data = resp.json().get("data", []) if resp.status_code == 200 else []
    if not data:
        # Fall back to general filter
        resp = _get(client, "/songs", {"order": "title", "per_page": per_page})
        resp.raise_for_status()
        data = resp.json().get("data", [])
    return [
        {
            "id": s["id"],
            "title": s["attributes"].get("title"),
            "author": s["attributes"].get("author"),
            "ccli_number": s["attributes"].get("ccli_number"),
            "account": account,
            "org_name": org,
        }
        for s in data
    ]


@mcp.tool()
def pco_search_songs(
    query: str,
    per_page: int = 10,
    account: Optional[str] = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Search for songs in the Planning Center library by title or keyword.

    Account routing: omitted `account` uses this chat's selection
    (`pco_use_account`), else the installation default; an explicit name always
    overrides; `"all"` fans out over every configured org.
    """
    return _dispatch(account, lambda name: _search_songs_single(query, per_page, name))


def _get_song_single(song_id: str | int, account: str) -> dict[str, Any]:
    sid = str(song_id).strip()
    client = get_client(account)
    org = org_name(account)
    resp = _get(client, f"/songs/{sid}", {"include": "arrangements"})
    resp.raise_for_status()
    raw = resp.json()
    song_attrs = raw.get("data", {}).get("attributes", {})

    arrangements = []
    for inc in raw.get("included", []):
        if inc.get("type") == "Arrangement":
            attrs = inc.get("attributes", {})
            arrangements.append({
                "id": inc.get("id"),
                "name": attrs.get("name"),
                "bpm": attrs.get("bpm"),
                "meter": attrs.get("meter"),
                "default_key": attrs.get("default_key"),
                "notes": attrs.get("notes"),
            })

    return {
        "id": sid,
        "title": song_attrs.get("title"),
        "author": song_attrs.get("author"),
        "ccli_number": song_attrs.get("ccli_number"),
        "copyright": song_attrs.get("copyright"),
        "arrangements": arrangements,
        "account": account,
        "org_name": org,
    }


@mcp.tool()
def pco_get_song(
    song_id: str | int,
    account: Optional[str] = None,
) -> dict[str, Any]:
    """Retrieve details for a specific song including all arrangements, keys, and metadata.

    Account routing: omitted `account` uses this chat's selection
    (`pco_use_account`), else the installation default; an explicit name always
    overrides; `"all"` fans out over every configured org.
    """
    return _dispatch(account, lambda name: _get_song_single(song_id, name))


# ---------------------------------------------------------------------------
# Person-centric history
# ---------------------------------------------------------------------------

STATUS_LABELS = {"C": "Confirmed", "U": "Unconfirmed", "D": "Declined"}

# Per-call counters, surfaced in the tool result as evidence of throttling.
_RETRY_STATS: dict[str, int] = {"requests": 0, "retries": 0, "rate_limited": 0}
_RETRY_STATUSES = {429, 500, 502, 503, 504}
# PCO allows 100 requests per 20s per app (x-pco-api-request-rate-*); stay under it.
_MIN_REQUEST_INTERVAL = 0.25
_LAST_REQUEST_AT = [0.0]
# Serializes pacing across concurrent tool calls (and fan-out is sequential).
_PACE_LOCK = threading.Lock()


def _pace() -> None:
    """Space requests so a full history scan does not trip the account rate limit."""
    with _PACE_LOCK:
        now = time.monotonic()
        wait = _MIN_REQUEST_INTERVAL - (now - _LAST_REQUEST_AT[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_AT[0] = time.monotonic()


def _get(
    client: httpx.Client,
    path: str,
    params: Optional[dict[str, Any]] = None,
    attempts: int = 6,
) -> httpx.Response:
    """GET paced under the account rate limit, with backoff on 429/5xx."""
    delay = 1.0
    resp: Optional[httpx.Response] = None
    for attempt in range(attempts):
        _pace()
        _RETRY_STATS["requests"] += 1
        try:
            resp = client.get(path, params=params)
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
            _RETRY_STATS["retries"] += 1
            time.sleep(delay)
            delay *= 2
            continue
        if resp.status_code == 429:
            _RETRY_STATS["rate_limited"] += 1
        if resp.status_code in _RETRY_STATUSES and attempt < attempts - 1:
            _RETRY_STATS["retries"] += 1
            wait = delay
            retry_after = resp.headers.get("retry-after")
            if retry_after:
                try:
                    wait = max(wait, float(retry_after))
                except ValueError:
                    pass
            time.sleep(wait)
            delay *= 2
            continue
        return resp
    assert resp is not None
    return resp


def _normalize_person_name(name: str) -> str:
    """Casefold and collapse whitespace so ' Alex  Morgan ' == 'alex morgan'."""
    return " ".join(str(name or "").replace("\u00a0", " ").split()).casefold()


def _person_name_variants(name: str) -> set[str]:
    """Accept 'First Last' and 'Last, First' spellings of the same person."""
    normalized = _normalize_person_name(name)
    variants: set[str] = {normalized} if normalized else set()
    if "," in normalized:
        last, _, first = normalized.partition(",")
        variants.add(_normalize_person_name(f"{first} {last}"))
    else:
        parts = normalized.split()
        if len(parts) >= 2:
            variants.add(_normalize_person_name(f"{parts[-1]}, {' '.join(parts[:-1])}"))
    return variants


def _iter_pages(
    client: httpx.Client,
    path: str,
    params: Optional[dict[str, Any]] = None,
    max_pages: int = 100,
):
    """Yield every row of a paginated collection (per_page=100, offset based)."""
    offset = 0
    for _ in range(max_pages):
        query: dict[str, Any] = dict(params or {})
        query["per_page"] = 100
        query["offset"] = offset
        resp = _get(client, path, query)
        if resp.status_code != 200:
            raise RuntimeError(
                f"GET {path} failed ({resp.status_code}): {resp.text[:200]}"
            )
        data = resp.json().get("data", [])
        if not data:
            return
        yield from data
        offset += len(data)
        if len(data) < 100:
            return


def _lookup_people(client: httpx.Client, person_name: str) -> Optional[list[dict[str, Any]]]:
    """Resolve a name against the account's Services people.

    Returns None when the lookup itself is unavailable (caller then falls back
    to matching team member names); otherwise the list of matching people.
    """
    try:
        resp = _get(client, "/people", {"where[name_like]": person_name, "per_page": 100})
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    people = []
    for row in resp.json().get("data", []):
        attrs = row.get("attributes", {})
        full_name = attrs.get("full_name") or " ".join(
            filter(None, [attrs.get("first_name"), attrs.get("last_name")])
        )
        people.append({
            "id": str(row.get("id")),
            "name": full_name,
            "archived": attrs.get("archived"),
        })
    return people


def _match_team_members(
    client: httpx.Client,
    service_type_id: str,
    plan_id: str,
    person_id: Optional[str],
    variants: set[str],
) -> list[dict[str, Any]]:
    """Return this person's team member rows on one plan."""
    matches = []
    path = f"/service_types/{service_type_id}/plans/{plan_id}/team_members"
    for member in _iter_pages(client, path, {"include": "person"}):
        attrs = member.get("attributes", {})
        person_rel = (
            member.get("relationships", {}).get("person", {}).get("data") or {}
        )
        member_person_id = str(person_rel.get("id") or "")
        member_name = attrs.get("name") or ""
        id_hit = bool(person_id and member_person_id == person_id)
        name_hit = _normalize_person_name(member_name) in variants
        if id_hit or name_hit:
            status = attrs.get("status")
            matches.append({
                "plan_person_id": str(member.get("id")),
                "person_id": member_person_id or None,
                "name": member_name,
                "position": attrs.get("team_position_name"),
                "status": status,
                "status_label": STATUS_LABELS.get(status, status),
                "decline_reason": attrs.get("decline_reason"),
                "status_updated_at": attrs.get("status_updated_at"),
            })
    return matches


def _fetch_plan_songs(
    client: httpx.Client,
    service_type_id: str,
    plan_id: str,
) -> list[dict[str, Any]]:
    """Song items of one plan with the key each song was scheduled in."""
    path = f"/service_types/{service_type_id}/plans/{plan_id}/items"
    included: dict[str, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = _get(
            client,
            path,
            {
                "per_page": 100,
                "offset": offset,
                "include": "song,arrangement,key",
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"GET {path} failed ({resp.status_code}): {resp.text[:200]}"
            )
        payload = resp.json()
        for inc in payload.get("included", []):
            included[f"{inc.get('type')}:{inc.get('id')}"] = inc
        data = payload.get("data", [])
        items.extend(data)
        if len(data) < 100:
            break
        offset += len(data)

    songs = []
    for item in items:
        attrs = item.get("attributes", {})
        if attrs.get("item_type") != "song":
            continue
        relationships = item.get("relationships", {})
        song_rel = (relationships.get("song", {}) or {}).get("data") or {}
        arr_rel = (relationships.get("arrangement", {}) or {}).get("data") or {}
        key_rel = (relationships.get("key", {}) or {}).get("data") or {}

        song_attrs = included.get(f"Song:{song_rel.get('id')}", {}).get("attributes", {})
        arr_attrs = included.get(f"Arrangement:{arr_rel.get('id')}", {}).get("attributes", {})
        key_attrs = included.get(f"Key:{key_rel.get('id')}", {}).get("attributes", {})

        key_name = (
            attrs.get("key_name")
            or key_attrs.get("name")
            or key_attrs.get("starting_key")
            or arr_attrs.get("default_key")
        )
        songs.append({
            "sequence": attrs.get("sequence"),
            "title": attrs.get("title") or song_attrs.get("title") or "Untitled",
            "key": key_name,
            "arrangement": arr_attrs.get("name"),
            "arrangement_id": str(arr_rel.get("id")) if arr_rel.get("id") else None,
            "song_id": str(song_rel.get("id")) if song_rel.get("id") else None,
            "ccli": song_attrs.get("ccli_number"),
        })
    songs.sort(key=lambda s: (s.get("sequence") is None, s.get("sequence") or 0))
    return songs


def _person_song_history_single(
    account: str,
    name: str,
    variants: set[str],
    service_type_id: Optional[str],
    limit: int,
    max_plans: int,
    include_declined: bool,
) -> dict[str, Any]:
    org = org_name(account)
    client = get_client(account)
    _RETRY_STATS.update({"requests": 0, "retries": 0, "rate_limited": 0})

    # 1. Service types for this account (or the one requested).
    service_types: list[dict[str, Any]] = []
    if service_type_id:
        resp = _get(client, f"/service_types/{service_type_id}")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Service type {service_type_id} not found under account '{account}' "
                f"({resp.status_code})"
            )
        st = resp.json().get("data", {})
        service_types = [{
            "id": str(st.get("id")),
            "name": st.get("attributes", {}).get("name"),
        }]
    else:
        try:
            for st in _iter_pages(client, "/service_types"):
                service_types.append({
                    "id": str(st.get("id")),
                    "name": st.get("attributes", {}).get("name"),
                })
        except RuntimeError as exc:
            raise RuntimeError(f"Could not list service types: {exc}") from exc

    if not service_types:
        raise RuntimeError(f"No service types found under account '{account}'")

    # 2. Resolve the person so plans are matched by id when possible.
    people = _lookup_people(client, name)
    person_id: Optional[str] = None
    exact = [
        p for p in (people or [])
        if _normalize_person_name(p.get("name")) in variants
    ]
    if len(exact) > 1:
        return {
            "account": account,
            "org_name": org,
            "person_name": name,
            "ambiguous": True,
            "matched_plans": 0,
            "returned_plans": 0,
            "scan_complete": False,
            "scanned_plans": 0,
            "count": 0,
            "candidates": exact,
            "plans": [],
            "note": "Multiple people match this name; narrow the name or use a person id.",
        }
    if len(exact) == 1:
        person_id = exact[0]["id"]
    elif people is not None:
        # Name resolved to nothing: report near matches rather than guessing.
        return {
            "account": account,
            "org_name": org,
            "person_name": name,
            "ambiguous": False,
            "matched_plans": 0,
            "returned_plans": 0,
            "scan_complete": False,
            "scanned_plans": 0,
            "count": 0,
            "candidates": people[:20],
            "plans": [],
            "note": "No exact person match for this account; candidates are near matches.",
        }

    # 3. Collect past plans, newest first.
    stubs: list[dict[str, Any]] = []
    skipped: list[str] = []
    failed_sts = 0
    for st in service_types:
        collected_for_st = 0
        try:
            for plan in _iter_pages(
                client,
                f"/service_types/{st['id']}/plans",
                {"filter": "past", "order": "-sort_date"},
            ):
                attrs = plan.get("attributes", {})
                stubs.append({
                    "id": str(plan.get("id")),
                    "service_type_id": st["id"],
                    "service_type_name": st["name"],
                    "title": attrs.get("title") or attrs.get("dates"),
                    "dates": attrs.get("dates"),
                    "sort_date": attrs.get("sort_date"),
                })
                collected_for_st += 1
                if collected_for_st >= max_plans:
                    break
        except (RuntimeError, httpx.HTTPError) as exc:
            failed_sts += 1
            skipped.append(f"{st['name']} ({st['id']}): {exc}")

    if not stubs and failed_sts == len(service_types):
        raise RuntimeError(
            "Could not read past plans for any service type: " + "; ".join(skipped)
        )

    stubs.sort(key=lambda p: p.get("sort_date") or "", reverse=True)
    stubs = stubs[:max_plans]

    # 4. Walk newest-first, matching the person and collecting songs.
    plans_out: list[dict[str, Any]] = []
    scanned = 0
    songs_total = 0
    matched_plans = 0
    scan_complete = True
    for stub in stubs:
        if songs_total >= limit:
            scan_complete = False
            break
        scanned += 1
        try:
            matches = _match_team_members(
                client, stub["service_type_id"], stub["id"], person_id, variants
            )
        except (RuntimeError, httpx.HTTPError) as exc:
            skipped.append(f"plan {stub['id']}: {exc}")
            continue
        if not matches:
            continue
        if not include_declined:
            matches = [m for m in matches if m.get("status") != "D"]
            if not matches:
                continue

        matched_plans += 1

        positions: list[str] = []
        statuses: list[str] = []
        for match in matches:
            position = match.get("position")
            if position and position not in positions:
                positions.append(position)
            status = match.get("status")
            if status and status not in statuses:
                statuses.append(status)

        plan_songs: list[dict[str, Any]] = []
        try:
            plan_songs = _fetch_plan_songs(
                client, stub["service_type_id"], stub["id"]
            )
        except (RuntimeError, httpx.HTTPError) as exc:
            skipped.append(f"plan {stub['id']} items: {exc}")

        remaining = limit - songs_total
        plan_songs = plan_songs[:remaining]
        songs_total += len(plan_songs)

        plans_out.append({
            "plan_id": stub["id"],
            "date": stub["sort_date"],
            "dates": stub["dates"],
            "title": stub["title"],
            "service_type_id": stub["service_type_id"],
            "service_type_name": stub["service_type_name"],
            "positions": positions,
            "statuses": statuses,
            "assignments": matches,
            "songs": plan_songs,
            "account": account,
            "org_name": org,
        })

    return {
        "account": account,
        "org_name": org,
        "person_name": name,
        "person_id": person_id,
        "matched_plans": matched_plans,
        "returned_plans": len(plans_out),
        "scan_complete": scan_complete,
        "scanned_plans": scanned,
        "count": songs_total,
        "plans": plans_out,
        "skipped": skipped,
        "requests": _RETRY_STATS["requests"],
        "retries": _RETRY_STATS["retries"],
        "rate_limited": _RETRY_STATS["rate_limited"],
    }


@mcp.tool()
def pco_person_song_history(
    account: Optional[str] = None,
    person_name: str = "",
    service_type_id: Optional[str] = None,
    limit: int = 15,
    max_plans: int = 320,
    include_declined: bool = False,
) -> dict[str, Any]:
    """List the songs a person was scheduled for, newest first, with the key each song was in.

    Scans past plans newest-first (all service types unless service_type_id is given),
    matches the person in each plan's team members (name, case/whitespace insensitive,
    'Last, First' accepted) and returns that plan's song items. limit counts SONGS;
    max_plans caps how many past plans are scanned. matched_plans and scan_complete
    are scoped to the scanned window: matched_plans counts the plans where the person
    matched inside that window, and scan_complete is True only when the scan ended
    because plans ran out (or max_plans was reached) rather than because limit songs
    were collected. limit therefore also controls scan depth — pass a higher limit
    (or max_plans) to scan deeper. returned_plans is how many matched plans are in
    plans. Assignments the person declined (status D) are skipped unless
    include_declined=True. Returns candidates instead of guessing when the name
    matches more than one person.

    Account routing: omitted `account` uses this chat's selection
    (`pco_use_account`), else the installation default; an explicit name always
    overrides; `"all"` fans out over every configured org.
    """
    name = (person_name or "").strip()
    if not name:
        raise ValueError("person_name is required")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if max_plans < 1:
        raise ValueError("max_plans must be >= 1")

    variants = _person_name_variants(name)
    return _dispatch(
        account,
        lambda resolved: _person_song_history_single(
            resolved, name, variants, service_type_id, limit, max_plans, include_declined
        ),
    )


def _parse_sort_date(value: Any) -> Optional[date]:
    """Parse a PCO sort_date (ISO date or ISO datetime); None when absent/invalid."""
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _person_upcoming_single(
    account: str,
    name: str,
    variants: set[str],
    service_type_id: Optional[str],
    day_from: date,
    day_to: date,
    days: int,
    include_declined: bool,
    max_plans: int,
) -> dict[str, Any]:
    org = org_name(account)
    client = get_client(account)
    _RETRY_STATS.update({"requests": 0, "retries": 0, "rate_limited": 0})
    window = {"from": day_from.isoformat(), "to": day_to.isoformat(), "days": days}

    def _result(**extra: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "account": account,
            "org_name": org,
            "person_name": name,
            "person_id": None,
            "ambiguous": False,
            "candidates": [],
            "window": window,
            "matched_plans": 0,
            "returned_plans": 0,
            "assignments_count": 0,
            "scanned_plans": 0,
            "scan_complete": False,
            "plans": [],
            "skipped": [],
        }
        result.update(extra)
        result.update({
            "requests": _RETRY_STATS["requests"],
            "retries": _RETRY_STATS["retries"],
            "rate_limited": _RETRY_STATS["rate_limited"],
        })
        return result

    # 1. Resolve the person first: ambiguity or a total miss needs no plan scan.
    people = _lookup_people(client, name)
    person_id: Optional[str] = None
    exact = [
        p for p in (people or [])
        if _normalize_person_name(p.get("name")) in variants
    ]
    if len(exact) > 1:
        return _result(
            ambiguous=True,
            candidates=exact,
            note="Multiple people match this name; narrow the name or use a person id.",
        )
    if len(exact) == 1:
        person_id = exact[0]["id"]
    elif people is not None:
        return _result(
            candidates=people[:20],
            note="No exact person match for this account; candidates are near matches.",
        )
    # people is None: the lookup is unavailable, so fall back to name matching
    # against team members (variants are always passed to _match_team_members).

    # 2. Service types for this account (or the one requested).
    service_types: list[dict[str, Any]] = []
    if service_type_id:
        resp = _get(client, f"/service_types/{service_type_id}")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Service type {service_type_id} not found under account '{account}' "
                f"({resp.status_code}): {resp.text[:200]}"
            )
        st = resp.json().get("data") or {}
        service_types = [{
            "id": str(st.get("id")),
            "name": (st.get("attributes") or {}).get("name"),
        }]
    else:
        try:
            for st in _iter_pages(client, "/service_types"):
                service_types.append({
                    "id": str(st.get("id")),
                    "name": (st.get("attributes") or {}).get("name"),
                })
        except RuntimeError as exc:
            raise RuntimeError(f"Could not list service types: {exc}") from exc

    if not service_types:
        raise RuntimeError(f"No service types found under account '{account}'")

    # 3. Collect future plans inside the window, oldest first per service type.
    stubs: list[dict[str, Any]] = []
    skipped: list[str] = []
    failed_sts = 0
    for st in service_types:
        try:
            for plan in _iter_pages(
                client,
                f"/service_types/{st['id']}/plans",
                {"filter": "future", "order": "sort_date"},
            ):
                attrs = plan.get("attributes") or {}
                sort_date = attrs.get("sort_date")
                parsed = _parse_sort_date(sort_date)
                if parsed is None:
                    skipped.append(
                        f"plan {plan.get('id')} ({st['name']}): "
                        f"missing or unparseable sort_date {sort_date!r}"
                    )
                    continue
                if parsed > day_to:
                    break
                if parsed < day_from:
                    continue
                stubs.append({
                    "id": str(plan.get("id")),
                    "service_type_id": st["id"],
                    "service_type_name": st["name"],
                    "title": attrs.get("title") or attrs.get("dates"),
                    "dates": attrs.get("dates"),
                    "sort_date": sort_date,
                    "date": parsed.isoformat(),
                })
        except (RuntimeError, httpx.HTTPError) as exc:
            failed_sts += 1
            skipped.append(f"{st['name']} ({st['id']}): {exc}")

    # Raise only when nothing at all could be read; partial rows stay useful.
    if not stubs and failed_sts == len(service_types):
        raise RuntimeError(
            "Could not read future plans for any service type: " + "; ".join(skipped)
        )

    stubs.sort(key=lambda p: (p["date"], p["service_type_name"] or "", p["id"]))
    truncated = len(stubs) > max_plans
    if truncated:
        stubs = stubs[:max_plans]

    # 4. Match the person on each plan and pull plan times only for hits.
    plans_out: list[dict[str, Any]] = []
    scanned = 0
    assignments_count = 0
    plan_scan_errors = 0
    for stub in stubs:
        scanned += 1
        try:
            matches = _match_team_members(
                client, stub["service_type_id"], stub["id"], person_id, variants
            )
        except (RuntimeError, httpx.HTTPError) as exc:
            plan_scan_errors += 1
            skipped.append(f"plan {stub['id']}: {exc}")
            continue
        if not matches:
            continue
        if not include_declined:
            matches = [m for m in matches if m.get("status") != "D"]
            if not matches:
                continue

        plan_times, plan_times_error = _fetch_plan_times(
            client, stub["service_type_id"], stub["id"]
        )
        starts_at = None
        for row in plan_times:
            if row.get("time_type") == "service" and row.get("starts_at"):
                starts_at = row["starts_at"]
                break
        if starts_at is None:
            for row in plan_times:
                if row.get("starts_at"):
                    starts_at = row["starts_at"]
                    break

        positions: list[str] = []
        statuses: list[str] = []
        for match in matches:
            position = match.get("position")
            if position and position not in positions:
                positions.append(position)
            status = match.get("status")
            if status and status not in statuses:
                statuses.append(status)

        plan: dict[str, Any] = {
            "plan_id": stub["id"],
            "service_type_id": stub["service_type_id"],
            "service_type_name": stub["service_type_name"],
            "title": stub["title"],
            "dates": stub["dates"],
            "sort_date": stub["sort_date"],
            "date": stub["date"],
            "starts_at": starts_at,
            "plan_times": plan_times,
            "positions": positions,
            "statuses": statuses,
            "assignments": matches,
            "account": account,
            "org_name": org,
        }
        if plan_times_error:
            plan["plan_times_error"] = plan_times_error
        plans_out.append(plan)
        assignments_count += len(matches)

    # A complete scan means no max_plans truncation, every service type's plan
    # listing succeeded, and every collected plan's team_members read succeeded.
    # plan_times failures are reported per plan and do not clear scan_complete.
    scan_complete = not truncated and failed_sts == 0 and plan_scan_errors == 0
    return _result(
        person_id=person_id,
        matched_plans=len(plans_out),
        returned_plans=len(plans_out),
        assignments_count=assignments_count,
        scanned_plans=scanned,
        scan_complete=scan_complete,
        plans=plans_out,
        skipped=skipped,
    )


@mcp.tool()
def pco_person_upcoming(
    person_name: str = "",
    days: int = 21,
    account: Optional[str] = None,
    service_type_id: Optional[str] = None,
    include_declined: bool = False,
    max_plans: int = 80,
) -> dict[str, Any]:
    """List the future plans a person is scheduled for, soonest first.

    Scans future plans from today through today+days (inclusive) across all service
    types unless service_type_id is given, matching the person in each plan's team
    members (name, case/whitespace insensitive, 'Last, First' accepted; person id is
    used when the name resolves to exactly one Services person). Plans are capped at
    max_plans total after sorting by date; scan_complete is False when that cap
    truncated the scan or when any service-type plan listing or plan team_members
    request failed (the failures are listed in skipped). Ambiguous or unmatched names
    return candidates and never scan plans. Assignments the person declined (status D)
    are dropped unless include_declined=True; a plan left with no assignments is
    skipped. Plan times and the service start (starts_at) are fetched only for
    matched plans; a plan-times failure is reported per plan via plan_times_error
    instead of failing the call.
    Read-only: only GETs are issued.

    Account routing: omitted `account` uses this chat's selection
    (`pco_use_account`), else the installation default; an explicit name always
    overrides; `"all"` fans out over every configured org and returns a merged,
    time-ordered `plans` list plus a `summary` block.
    """
    name = (person_name or "").strip()
    if not name:
        raise ValueError("person_name is required")
    if days < 1 or days > 366:
        raise ValueError("days must be between 1 and 366")
    if max_plans < 1:
        raise ValueError("max_plans must be >= 1")

    day_from = date.today()
    day_to = day_from + timedelta(days=days)
    window = {"from": day_from.isoformat(), "to": day_to.isoformat(), "days": days}
    variants = _person_name_variants(name)

    result = _dispatch(
        account,
        lambda resolved: _person_upcoming_single(
            resolved, name, variants, service_type_id,
            day_from, day_to, days, include_declined, max_plans,
        ),
    )

    if isinstance(result, dict) and result.get("account_source") == "all":
        plans: list[dict[str, Any]] = []
        matched = 0
        assignments = 0
        for entry in result.get("results") or []:
            data = entry.get("data") or {}
            plans.extend(data.get("plans") or [])
            matched += int(data.get("matched_plans") or 0)
            assignments += int(data.get("assignments_count") or 0)
        plans.sort(key=lambda p: (
            p.get("starts_at") or p.get("sort_date") or "",
            p.get("account") or "",
            p.get("service_type_name") or "",
        ))
        result.update({
            "person_name": name,
            "window": window,
            "plans": plans,
            "count": len(plans),
            "summary": {
                "accounts_queried": len(result.get("accounts") or []),
                "accounts_succeeded": len(result.get("results") or []),
                "accounts_failed": len(result.get("errors") or []),
                "matched_plans": matched,
                "assignments": assignments,
            },
        })
    return result


# ---------------------------------------------------------------------------
# Writes: explicit account only, preview unless confirm=True
# ---------------------------------------------------------------------------
#
# Every mutating HTTP call lives in pco_writes.run_write. These tools validate
# input, preflight the target under the same explicit account, and never consult
# pco_use_account, the installation default, or account="all".


def _write_attrs(obj: Any) -> dict[str, Any]:
    """Attributes of a preflight resource; accepts a flat row or JSON:API envelope."""
    if not isinstance(obj, dict):
        return {}
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    attrs = data.get("attributes") if isinstance(data, dict) else None
    if isinstance(attrs, dict):
        return attrs
    # Also accept a flattened {id, **attributes} mapping for compatibility with
    # preflight helpers that merge attributes over the resource id.
    if isinstance(data, dict):
        return {
            key: value
            for key, value in data.items()
            if key not in ("id", "type", "relationships", "links")
        }
    return {}


def _parse_instant(value: str) -> datetime:
    """Parse an ISO-8601 instant that clean_iso already validated."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _plan_summary(
    service_type_id: str,
    plan_id: str,
    attrs: dict[str, Any],
    account: str,
    org: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "resource": "plan",
        "service_type_id": service_type_id,
        "id": plan_id,
        "title": attrs.get("title"),
        "sort_date": attrs.get("sort_date"),
        **extra,
        "account": account,
        "org_name": org,
    }


def _item_summary(
    service_type_id: str,
    plan_id: str,
    item_id: str,
    attrs: dict[str, Any],
    account: str,
    org: str,
) -> dict[str, Any]:
    return {
        "resource": "item",
        "service_type_id": service_type_id,
        "plan_id": plan_id,
        "id": item_id,
        "title": attrs.get("title"),
        "item_type": attrs.get("item_type"),
        "sequence": attrs.get("sequence"),
        "account": account,
        "org_name": org,
    }


def _plan_time_summary(
    service_type_id: str,
    plan_id: str,
    plan_time_id: str,
    attrs: dict[str, Any],
    account: str,
    org: str,
) -> dict[str, Any]:
    return {
        "resource": "plan_time",
        "service_type_id": service_type_id,
        "plan_id": plan_id,
        "id": plan_time_id,
        "name": attrs.get("name"),
        "time_type": attrs.get("time_type"),
        "starts_at": attrs.get("starts_at"),
        "ends_at": attrs.get("ends_at"),
        "account": account,
        "org_name": org,
    }


def _find_plan_time(rows: list[dict[str, Any]], plan_time_id: str) -> Optional[dict[str, Any]]:
    for row in rows:
        if str(row.get("id")) == plan_time_id:
            return row
    return None


@mcp.tool()
def pco_create_plan(
    account: str,
    service_type_id: str,
    title: str,
    public: bool = False,
    series_title: Optional[str] = None,
    series_id: Optional[str] = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a plan under a service type.

    Writes require an explicit `account`; "all" and this chat's selection are
    rejected, never used as a fallback. confirm=False (default) returns the exact
    payload and preflight without writing; pass confirm=True to send it. The
    request is never retried, so a failed write must be inspected before retrying.
    """
    account, org = write_context(account)
    st_id = require_id(service_type_id, "service_type_id")
    attrs: dict[str, Any] = {
        "title": clean_text(title, "title"),
        "public": clean_bool(public, "public"),
    }
    if series_title is not None:
        cleaned_series = clean_text(series_title, "series_title", required=False)
        if cleaned_series is not None:
            attrs["series_title"] = cleaned_series
    if series_id is not None:
        attrs["series_id"] = require_id(series_id, "series_id")

    st_attrs = _write_attrs(preflight_object(account, f"/service_types/{st_id}"))

    return run_write(
        account=account,
        operation="create_plan",
        method="POST",
        path=f"/service_types/{st_id}/plans",
        resource_type="Plan",
        resource_id=None,
        attributes=attrs,
        preflight={
            "resource": "service_type",
            "id": st_id,
            "name": st_attrs.get("name"),
            "account": account,
            "org_name": org,
        },
        confirm=confirm,
    )


@mcp.tool()
def pco_update_plan(
    account: str,
    service_type_id: str,
    plan_id: str,
    title: Optional[str] = None,
    public: Optional[bool] = None,
    series_title: Optional[str] = None,
    series_id: Optional[str] = None,
    reminders_disabled: Optional[bool] = None,
    expected_title: Optional[str] = None,
    expected_sort_date: Optional[str] = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Update plan fields; at least one attribute is required.

    Writes require an explicit `account`; "all" and this chat's selection are
    rejected. expected_title/expected_sort_date, when given, must match the
    current plan exactly before anything is sent. confirm=False (default)
    previews; confirm=True sends one request. Never retried.
    """
    account, org = write_context(account)
    st_id = require_id(service_type_id, "service_type_id")
    plan = require_id(plan_id, "plan_id")
    attrs: dict[str, Any] = {}
    if title is not None:
        attrs["title"] = clean_text(title, "title")
    if public is not None:
        attrs["public"] = clean_bool(public, "public")
    if series_title is not None:
        cleaned_series = clean_text(series_title, "series_title", required=False)
        if cleaned_series is not None:
            attrs["series_title"] = cleaned_series
    if series_id is not None:
        attrs["series_id"] = require_id(series_id, "series_id")
    if reminders_disabled is not None:
        attrs["reminders_disabled"] = clean_bool(reminders_disabled, "reminders_disabled")
    if not attrs:
        raise ValueError("Provide at least one plan attribute to update")

    plan_attrs = _write_attrs(preflight_object(account, f"/service_types/{st_id}/plans/{plan}"))
    guard = clean_text(expected_title, "expected_title", required=False) if expected_title is not None else None
    if guard is not None:
        check_expected_text(plan_attrs.get("title"), guard, "plan title")
    if expected_sort_date is not None:
        check_expected_date(
            plan_attrs.get("sort_date"),
            expected_sort_date,
            "plan sort_date",
        )

    return run_write(
        account=account,
        operation="update_plan",
        method="PATCH",
        path=f"/service_types/{st_id}/plans/{plan}",
        resource_type="Plan",
        resource_id=plan,
        attributes=attrs,
        preflight=_plan_summary(st_id, plan, plan_attrs, account, org),
        confirm=confirm,
    )


@mcp.tool()
def pco_delete_plan(
    account: str,
    service_type_id: str,
    plan_id: str,
    expected_title: Optional[str] = None,
    expected_sort_date: Optional[str] = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete a plan; requires an exact title or sort_date guard.

    Pass expected_title or expected_sort_date (at least one). Every guard supplied
    must match the current plan exactly before anything is sent; sort_date works
    alone for plans with a null title. Writes require an explicit `account`;
    "all" and this chat's selection are rejected. confirm=False (default)
    previews; confirm=True sends one request. Never retried.
    """
    account, org = write_context(account)
    st_id = require_id(service_type_id, "service_type_id")
    plan = require_id(plan_id, "plan_id")
    title_guard = clean_text(expected_title, "expected_title", required=False) if expected_title is not None else None
    date_guard = clean_text(expected_sort_date, "expected_sort_date", required=False) if expected_sort_date is not None else None
    if title_guard is None and date_guard is None:
        raise ValueError("Pass expected_title or expected_sort_date so the delete is verified")

    plan_attrs = _write_attrs(preflight_object(account, f"/service_types/{st_id}/plans/{plan}"))
    if title_guard is not None:
        check_expected_text(plan_attrs.get("title"), title_guard, "plan title")
    if date_guard is not None:
        check_expected_date(plan_attrs.get("sort_date"), date_guard, "plan sort_date")

    return run_write(
        account=account,
        operation="delete_plan",
        method="DELETE",
        path=f"/service_types/{st_id}/plans/{plan}",
        resource_type="Plan",
        resource_id=plan,
        attributes=None,
        preflight=_plan_summary(st_id, plan, plan_attrs, account, org),
        confirm=confirm,
    )


@mcp.tool()
def pco_create_plan_item(
    account: str,
    service_type_id: str,
    plan_id: str,
    title: str,
    item_type: str = "item",
    song_id: Optional[str] = None,
    arrangement_id: Optional[str] = None,
    key_id: Optional[str] = None,
    length: Optional[int] = None,
    description: Optional[str] = None,
    service_position: str = "during",
    sequence: Optional[int] = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create an item (item, header, or song) on a plan.

    item_type is item|header|song; a song_id with item_type="item" is treated as
    a song. song requires song_id; arrangement_id/key_id require song_id. A song
    preflights /songs/{song_id} under the same account. Writes require an explicit
    `account`; "all" and this chat's selection are rejected. confirm=False
    (default) previews; confirm=True sends one request. Never retried.
    """
    account, org = write_context(account)
    st_id = require_id(service_type_id, "service_type_id")
    plan = require_id(plan_id, "plan_id")
    kind = (item_type or "").strip().lower()
    if kind not in ("item", "header", "song"):
        raise ValueError("item_type must be 'item', 'header', or 'song'")
    song = require_id(song_id, "song_id") if song_id is not None else None
    if kind == "item" and song is not None:
        kind = "song"
    if kind == "song" and song is None:
        raise ValueError("song_id is required when item_type='song'")
    if kind == "header" and song is not None:
        raise ValueError("header items cannot include song_id")
    if song is None and (arrangement_id is not None or key_id is not None):
        raise ValueError("arrangement_id and key_id require song_id")
    arrangement = require_id(arrangement_id, "arrangement_id") if arrangement_id is not None else None
    key = require_id(key_id, "key_id") if key_id is not None else None

    attrs: dict[str, Any] = {
        "title": clean_text(title, "title"),
        "service_position": clean_service_position(service_position),
    }
    if kind == "header":
        attrs["item_type"] = "header"
    if song is not None:
        attrs["song_id"] = song
        if arrangement is not None:
            attrs["arrangement_id"] = arrangement
        if key is not None:
            attrs["key_id"] = key
    if length is not None:
        attrs["length"] = clean_int(length, "length", minimum=0, optional=True)
    if description is not None:
        cleaned_description = clean_text(description, "description", required=False)
        if cleaned_description is not None:
            attrs["description"] = cleaned_description
    if sequence is not None:
        attrs["sequence"] = clean_int(sequence, "sequence", minimum=0, optional=True)

    plan_attrs = _write_attrs(preflight_object(account, f"/service_types/{st_id}/plans/{plan}"))
    extra: dict[str, Any] = {"item_type": kind, "song_id": song}
    if song is not None:
        song_attrs = _write_attrs(preflight_object(account, f"/songs/{song}"))
        extra["song_title"] = song_attrs.get("title")

    return run_write(
        account=account,
        operation="create_plan_item",
        method="POST",
        path=f"/service_types/{st_id}/plans/{plan}/items",
        resource_type="Item",
        resource_id=None,
        attributes=attrs,
        preflight=_plan_summary(st_id, plan, plan_attrs, account, org, **extra),
        confirm=confirm,
    )


@mcp.tool()
def pco_update_plan_item(
    account: str,
    service_type_id: str,
    plan_id: str,
    item_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    length: Optional[int] = None,
    service_position: Optional[str] = None,
    sequence: Optional[int] = None,
    expected_title: Optional[str] = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Update plan item fields; at least one attribute is required.

    expected_title, when given, must match the current item title exactly before
    anything is sent. Writes require an explicit `account`; "all" and this chat's
    selection are rejected. confirm=False (default) previews; confirm=True sends
    one request. Never retried.
    """
    account, org = write_context(account)
    st_id = require_id(service_type_id, "service_type_id")
    plan = require_id(plan_id, "plan_id")
    item = require_id(item_id, "item_id")
    attrs: dict[str, Any] = {}
    if title is not None:
        attrs["title"] = clean_text(title, "title")
    if description is not None:
        cleaned_description = clean_text(description, "description", required=False)
        if cleaned_description is not None:
            attrs["description"] = cleaned_description
    if length is not None:
        attrs["length"] = clean_int(length, "length", minimum=0, optional=True)
    if service_position is not None:
        attrs["service_position"] = clean_service_position(service_position)
    if sequence is not None:
        attrs["sequence"] = clean_int(sequence, "sequence", minimum=0, optional=True)
    if not attrs:
        raise ValueError("Provide at least one item attribute to update")

    item_attrs = _write_attrs(
        preflight_object(account, f"/service_types/{st_id}/plans/{plan}/items/{item}")
    )
    guard = clean_text(expected_title, "expected_title", required=False) if expected_title is not None else None
    if guard is not None:
        check_expected_text(item_attrs.get("title"), guard, "item title")

    return run_write(
        account=account,
        operation="update_plan_item",
        method="PATCH",
        path=f"/service_types/{st_id}/plans/{plan}/items/{item}",
        resource_type="Item",
        resource_id=item,
        attributes=attrs,
        preflight=_item_summary(st_id, plan, item, item_attrs, account, org),
        confirm=confirm,
    )


@mcp.tool()
def pco_delete_plan_item(
    account: str,
    service_type_id: str,
    plan_id: str,
    item_id: str,
    expected_title: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete a plan item after an exact current-title check.

    Writes require an explicit `account`; "all" and this chat's selection are
    rejected. confirm=False (default) previews; confirm=True sends one request.
    Never retried.
    """
    account, org = write_context(account)
    st_id = require_id(service_type_id, "service_type_id")
    plan = require_id(plan_id, "plan_id")
    item = require_id(item_id, "item_id")
    guard = clean_text(expected_title, "expected_title")

    item_attrs = _write_attrs(
        preflight_object(account, f"/service_types/{st_id}/plans/{plan}/items/{item}")
    )
    check_expected_text(item_attrs.get("title"), guard, "item title")

    return run_write(
        account=account,
        operation="delete_plan_item",
        method="DELETE",
        path=f"/service_types/{st_id}/plans/{plan}/items/{item}",
        resource_type="Item",
        resource_id=item,
        attributes=None,
        preflight=_item_summary(st_id, plan, item, item_attrs, account, org),
        confirm=confirm,
    )


@mcp.tool()
def pco_create_plan_time(
    account: str,
    service_type_id: str,
    plan_id: str,
    starts_at: str,
    ends_at: str,
    time_type: str = "service",
    name: Optional[str] = None,
    team_reminders: Optional[dict[str, int]] = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a plan time (service, rehearsal, or other) on a plan.

    starts_at/ends_at must be ISO-8601 with an explicit timezone, and ends_at must
    be after starts_at. time_type is service|rehearsal|other; team_reminders maps
    numeric team ids to 0..7. Writes require an explicit `account`; "all" and this
    chat's selection are rejected. confirm=False (default) previews; confirm=True
    sends one request. Never retried.
    """
    account, org = write_context(account)
    st_id = require_id(service_type_id, "service_type_id")
    plan = require_id(plan_id, "plan_id")
    start = clean_iso(starts_at, "starts_at", required=True)
    end = clean_iso(ends_at, "ends_at", required=True)
    if _parse_instant(end) <= _parse_instant(start):
        raise ValueError("ends_at must be after starts_at")
    attrs: dict[str, Any] = {
        "starts_at": start,
        "ends_at": end,
        "time_type": clean_time_type(time_type),
    }
    if name is not None:
        cleaned_name = clean_text(name, "name", required=False)
        if cleaned_name is not None:
            attrs["name"] = cleaned_name
    if team_reminders is not None:
        attrs["team_reminders"] = clean_team_reminders(team_reminders)

    plan_attrs = _write_attrs(preflight_object(account, f"/service_types/{st_id}/plans/{plan}"))

    return run_write(
        account=account,
        operation="create_plan_time",
        method="POST",
        path=f"/service_types/{st_id}/plans/{plan}/plan_times",
        resource_type="PlanTime",
        resource_id=None,
        attributes=attrs,
        preflight=_plan_summary(
            st_id, plan, plan_attrs, account, org, starts_at=start, ends_at=end
        ),
        confirm=confirm,
    )


@mcp.tool()
def pco_update_plan_time(
    account: str,
    service_type_id: str,
    plan_id: str,
    plan_time_id: str,
    starts_at: Optional[str] = None,
    ends_at: Optional[str] = None,
    time_type: Optional[str] = None,
    name: Optional[str] = None,
    team_reminders: Optional[dict[str, int]] = None,
    expected_starts_at: Optional[str] = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Update a plan time; at least one attribute is required.

    The plan-time collection is preflighted under the same account and the id
    must exist. expected_starts_at, when given, must match the current instant
    exactly before anything is sent. Writes require an explicit `account`; "all"
    and this chat's selection are rejected. confirm=False (default) previews;
    confirm=True sends one request. Never retried.
    """
    account, org = write_context(account)
    st_id = require_id(service_type_id, "service_type_id")
    plan = require_id(plan_id, "plan_id")
    plan_time = require_id(plan_time_id, "plan_time_id")
    attrs: dict[str, Any] = {}
    if starts_at is not None:
        attrs["starts_at"] = clean_iso(starts_at, "starts_at", required=True)
    if ends_at is not None:
        attrs["ends_at"] = clean_iso(ends_at, "ends_at", required=True)
    if time_type is not None:
        attrs["time_type"] = clean_time_type(time_type)
    if name is not None:
        cleaned_name = clean_text(name, "name", required=False)
        if cleaned_name is not None:
            attrs["name"] = cleaned_name
    if team_reminders is not None:
        attrs["team_reminders"] = clean_team_reminders(team_reminders)
    if not attrs:
        raise ValueError("Provide at least one plan-time attribute to update")
    if "starts_at" in attrs and "ends_at" in attrs:
        if _parse_instant(attrs["ends_at"]) <= _parse_instant(attrs["starts_at"]):
            raise ValueError("ends_at must be after starts_at")

    rows = preflight_list(
        account,
        f"/service_types/{st_id}/plans/{plan}/plan_times",
        {"per_page": 100},
    )
    row = _find_plan_time(rows, plan_time)
    if row is None:
        raise ValueError(
            f"Plan time {plan_time} not found on plan {plan} under account "
            f"'{account}' ({org})"
        )
    row_attrs = _write_attrs(row)
    if "starts_at" in attrs or "ends_at" in attrs:
        effective_start = attrs.get("starts_at") or row_attrs.get("starts_at")
        effective_end = attrs.get("ends_at") or row_attrs.get("ends_at")
        if isinstance(effective_start, str) and isinstance(effective_end, str):
            if _parse_instant(effective_end) <= _parse_instant(effective_start):
                raise ValueError("ends_at must be after the plan time's starts_at")
    guard = clean_iso(expected_starts_at, "expected_starts_at", required=True) if expected_starts_at is not None else None
    if guard is not None:
        check_expected_time(row_attrs.get("starts_at"), guard, "plan time starts_at")

    return run_write(
        account=account,
        operation="update_plan_time",
        method="PATCH",
        path=f"/service_types/{st_id}/plan_times/{plan_time}",
        resource_type="PlanTime",
        resource_id=plan_time,
        attributes=attrs,
        preflight=_plan_time_summary(st_id, plan, plan_time, row_attrs, account, org),
        confirm=confirm,
    )


@mcp.tool()
def pco_delete_plan_time(
    account: str,
    service_type_id: str,
    plan_id: str,
    plan_time_id: str,
    expected_starts_at: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete a plan time after an exact current-start check.

    The plan-time collection is preflighted under the same account and the id
    must exist. Writes require an explicit `account`; "all" and this chat's
    selection are rejected. confirm=False (default) previews; confirm=True sends
    one request. Never retried.
    """
    account, org = write_context(account)
    st_id = require_id(service_type_id, "service_type_id")
    plan = require_id(plan_id, "plan_id")
    plan_time = require_id(plan_time_id, "plan_time_id")
    guard = clean_iso(expected_starts_at, "expected_starts_at", required=True)

    rows = preflight_list(
        account,
        f"/service_types/{st_id}/plans/{plan}/plan_times",
        {"per_page": 100},
    )
    row = _find_plan_time(rows, plan_time)
    if row is None:
        raise ValueError(
            f"Plan time {plan_time} not found on plan {plan} under account "
            f"'{account}' ({org})"
        )
    row_attrs = _write_attrs(row)
    check_expected_time(row_attrs.get("starts_at"), guard, "plan time starts_at")

    return run_write(
        account=account,
        operation="delete_plan_time",
        method="DELETE",
        path=f"/service_types/{st_id}/plan_times/{plan_time}",
        resource_type="PlanTime",
        resource_id=plan_time,
        attributes=None,
        preflight=_plan_time_summary(st_id, plan, plan_time, row_attrs, account, org),
        confirm=confirm,
    )


if __name__ == "__main__":
    mcp.run()
