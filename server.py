#!/usr/bin/env python3
"""Planning Center Online (PCO) Services MCP Server for omp.

Integrates with Planning Center Services API (v2) using credentials from
~/.mpco_credentials.json or environment variables.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from fastmcp import FastMCP

mcp = FastMCP("planning-center")

CREDS_FILE = Path(os.path.expanduser("~/.mpco_credentials.json"))
PCO_BASE_URL = "https://api.planningcenteronline.com/services/v2"


def get_credentials(account: str = "unw") -> tuple[str, str]:
    """Retrieve PCO appId and secret for the specified account."""
    env_id = os.environ.get("PCO_APP_ID")
    env_secret = os.environ.get("PCO_SECRET") or os.environ.get("PCO_APP_SECRET")
    if env_id and env_secret:
        return env_id, env_secret

    if CREDS_FILE.is_file():
        try:
            creds = json.loads(CREDS_FILE.read_text(encoding="utf-8"))
            if account in creds:
                acct = creds[account]
                return acct["appId"], acct.get("appSecret") or acct.get("secret")
            # Fall back to first available account
            if creds:
                first_key = next(iter(creds))
                acct = creds[first_key]
                return acct["appId"], acct.get("appSecret") or acct.get("secret")
        except Exception as e:
            raise RuntimeError(f"Failed to read {CREDS_FILE}: {e}")

    raise RuntimeError(
        f"No credentials found for account '{account}'. Please configure ~/.mpco_credentials.json or set PCO_APP_ID / PCO_SECRET."
    )


def make_client(account: str = "unw") -> httpx.Client:
    app_id, secret = get_credentials(account)
    auth_str = base64.b64encode(f"{app_id}:{secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_str}",
        "Accept": "application/json",
        "User-Agent": "PlanningCenter-MCP/1.0",
    }
    return httpx.Client(base_url=PCO_BASE_URL, headers=headers, timeout=30.0)


def extract_plan_id(plan_id_or_url: str | int) -> str:
    s = str(plan_id_or_url).strip()
    match = re.search(r"(\d{5,})", s)
    if match:
        return match.group(1)
    if s.isdigit():
        return s
    raise ValueError(f"Could not extract numeric plan ID from '{plan_id_or_url}'")


@mcp.tool()
def pco_list_accounts() -> list[str]:
    """List all configured Planning Center account names in ~/.mpco_credentials.json."""
    if not CREDS_FILE.is_file():
        return []
    try:
        creds = json.loads(CREDS_FILE.read_text(encoding="utf-8"))
        return list(creds.keys())
    except Exception:
        return []


@mcp.tool()
def pco_list_service_types(account: str = "unw") -> list[dict[str, Any]]:
    """List all service types (e.g. Sunday Morning, Youth, Chapels) for the specified account."""
    with make_client(account) as client:
        resp = client.get("/service_types?per_page=100")
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [
            {
                "id": st["id"],
                "name": st["attributes"].get("name"),
                "frequency": st["attributes"].get("frequency"),
            }
            for st in data
        ]


@mcp.tool()
def pco_list_plans(
    service_type_id: Optional[str] = None,
    filter: str = "future",
    per_page: int = 10,
    account: str = "unw",
) -> list[dict[str, Any]]:
    """List plans for a service type (or across service types if service_type_id is omitted).

    filter can be 'future', 'past', or 'all'.
    """
    with make_client(account) as client:
        st_ids: list[str] = []
        if service_type_id:
            st_ids = [str(service_type_id)]
        else:
            sts = client.get("/service_types?per_page=100").json().get("data", [])
            st_ids = [st["id"] for st in sts]

        plans_out = []
        for st_id in st_ids:
            query = f"/service_types/{st_id}/plans?per_page={per_page}&order=sort_date"
            if filter in ("future", "past"):
                query += f"&filter={filter}"
            resp = client.get(query)
            if resp.status_code != 200:
                continue
            for p in resp.json().get("data", []):
                attrs = p["attributes"]
                plans_out.append({
                    "id": p["id"],
                    "service_type_id": st_id,
                    "title": attrs.get("title") or attrs.get("dates"),
                    "dates": attrs.get("dates"),
                    "sort_date": attrs.get("sort_date"),
                })
        return plans_out


@mcp.tool()
def pco_get_plan(
    plan_id_or_url: str,
    account: str = "unw",
    include_attachments: bool = True,
) -> dict[str, Any]:
    """Retrieve full plan details, setlist order, songs, arrangements, keys, notes, and attachments from a plan ID or URL."""
    plan_id = extract_plan_id(plan_id_or_url)
    with make_client(account) as client:
        # Find which service_type contains this plan
        sts_resp = client.get("/service_types?per_page=100")
        sts_resp.raise_for_status()
        st_data = sts_resp.json().get("data", [])

        st_id = None
        plan_raw = None
        for st in st_data:
            cand_id = st["id"]
            check_resp = client.get(f"/service_types/{cand_id}/plans/{plan_id}")
            if check_resp.status_code == 200:
                st_id = cand_id
                plan_raw = check_resp.json().get("data")
                break

        if not st_id or not plan_raw:
            raise RuntimeError(f"Plan {plan_id} not found under account '{account}'")

        plan_attrs = plan_raw.get("attributes", {})
        plan_title = plan_attrs.get("title") or plan_attrs.get("dates") or f"Plan {plan_id}"

        # Fetch items with included song, arrangement, and key
        items_url = f"/service_types/{st_id}/plans/{plan_id}/items?include=song,arrangement,key,item_notes&per_page=100"
        items_resp = client.get(items_url)
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
                "sequence": sequence,
                "title": title,
                "type": item_type,
                "description": description,
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
                    att_resp = client.get(att_url)
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

        return {
            "plan_id": plan_id,
            "title": plan_title,
            "dates": plan_attrs.get("dates"),
            "sort_date": plan_attrs.get("sort_date"),
            "service_type_id": st_id,
            "items": items_out,
        }


@mcp.tool()
def pco_search_songs(
    query: str,
    per_page: int = 10,
    account: str = "unw",
) -> list[dict[str, Any]]:
    """Search for songs in the Planning Center library by title or keyword."""
    with make_client(account) as client:
        resp = client.get(f"/songs?where[title]={query}&per_page={per_page}")
        if resp.status_code != 200 or not resp.json().get("data"):
            # Fall back to general filter
            resp = client.get(f"/songs?order=title&per_page={per_page}")
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [
            {
                "id": s["id"],
                "title": s["attributes"].get("title"),
                "author": s["attributes"].get("author"),
                "ccli_number": s["attributes"].get("ccli_number"),
            }
            for s in data
        ]


@mcp.tool()
def pco_get_song(
    song_id: str | int,
    account: str = "unw",
) -> dict[str, Any]:
    """Retrieve details for a specific song including all arrangements, keys, and metadata."""
    sid = str(song_id).strip()
    with make_client(account) as client:
        resp = client.get(f"/songs/{sid}?include=arrangements")
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
        }


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


def _pace() -> None:
    """Space requests so a full history scan does not trip the account rate limit."""
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


def _require_known_account(account: str) -> None:
    """Fail clearly for a bad account name instead of the credentials fallback."""
    if os.environ.get("PCO_APP_ID") and (os.environ.get("PCO_SECRET") or os.environ.get("PCO_APP_SECRET")):
        return
    if not CREDS_FILE.is_file():
        return
    try:
        names = list(json.loads(CREDS_FILE.read_text(encoding="utf-8")).keys())
    except Exception:
        return
    if account not in names:
        raise RuntimeError(
            f"Unknown account '{account}'. Configured accounts: {', '.join(names)}"
        )


@mcp.tool()
def pco_person_song_history(
    account: str = "unw",
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
    """
    name = (person_name or "").strip()
    if not name:
        raise ValueError("person_name is required")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if max_plans < 1:
        raise ValueError("max_plans must be >= 1")

    _require_known_account(account)
    variants = _person_name_variants(name)
    _RETRY_STATS.update({"requests": 0, "retries": 0, "rate_limited": 0})

    with make_client(account) as client:
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
            })

        return {
            "account": account,
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


if __name__ == "__main__":
    mcp.run()
