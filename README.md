# pco-mcp

A [FastMCP](https://github.com/jlowin/fastmcp) stdio server for the **Planning Center Online Services API v2**. It exposes seven tools for reading service types, plans, setlists, songs, arrangements and a person's song history, and is registered with [omp](https://github.com/ompfine/omp) as `pco`.

Read-only: every tool issues `GET` against `https://api.planningcenteronline.com/services/v2`. Nothing is written back to Planning Center.

## Tools

All tools take an `account` argument (default `"unw"`) that selects a key in `~/.mpco_credentials.json`.

### `pco_list_accounts() -> list[str]`
Account names configured in `~/.mpco_credentials.json`. Useful as a first call to discover valid `account` values.

### `pco_list_service_types(account="unw") -> list[dict]`
Service types in the account (e.g. Sunday Morning, Youth, Chapels). Returns `id`, `name`.

### `pco_list_plans(service_type_id=None, filter="future", per_page=10, account="unw") -> list[dict]`
Plans for one service type, or across every service type when `service_type_id` is omitted. `filter` is `"future"`, `"past"`, or `"all"`. Returns `id`, `service_type_id`, `title`, `dates` (human-readable), `sort_date` (ISO, for sorting).

### `pco_get_plan(plan_id_or_url, account="unw", include_attachments=True) -> dict`
Full plan by numeric ID or by pasted Planning Center URL. Locates the owning service type, then returns plan metadata plus `items` sorted by `sequence`. Song items carry `song_id`, `song_title`, `author`, `ccli_number`, `arrangement_id`, `arrangement_name`, `key`, `bpm`, `meter`, `arrangement_notes`; set `include_attachments=False` to skip the per-song arrangement attachment lookups (this is the expensive part — one request per song item).

### `pco_search_songs(query, per_page=10, account="unw") -> list[dict]`
Library search by title/keyword. Falls back to a plain title-ordered listing when the filtered query returns nothing. Returns `id`, `title`, `author`, `ccli_number`.

### `pco_get_song(song_id, account="unw") -> dict`
One song with its arrangements (`name`, `bpm`, `meter`, `default_key`, `notes`) plus copyright and CCLI number.

### `pco_person_song_history(account="unw", person_name="", service_type_id=None, limit=15, max_plans=320, include_declined=False) -> dict`
The songs a person was scheduled for, **newest first**, with the key each song was in.

`person_name` is required and matched case- and whitespace-insensitively; `"Last, First"` is accepted as well as `"First Last"`. If several people match exactly, the tool returns `ambiguous: true` with `candidates` instead of guessing; if nobody matches exactly it returns `candidates` (near matches) and a note.

Returned shape:

```json
{
  "account": "unw",
  "person_name": "Alex Morgan",
  "person_id": "1234567",
  "matched_plans": 3,
  "returned_plans": 3,
  "scan_complete": false,
  "scanned_plans": 7,
  "count": 15,
  "plans": [
    {
      "plan_id": "...", "date": "2026-11-16", "dates": "November 16, 2026",
      "title": "Sunday Morning", "service_type_id": "...", "service_type_name": "Sunday Morning",
      "positions": ["Acoustic Guitar"], "statuses": ["C"],
      "assignments": [{"person_id": "...", "name": "...", "position": "...", "status": "C"}],
      "songs": [{"title": "...", "key": "G", "sequence": 2, "...": "..."}]
    }
  ],
  "skipped": [],
  "requests": 42,
  "retries": 0,
  "rate_limited": 0
}
```

Semantics worth knowing:

- **`limit` counts songs, not plans.** `count` is the number of song entries returned; each plan's `songs` list is truncated to what remains of the limit.
- **Declined assignments are excluded by default.** Team-member status is `C` (Confirmed), `U` (Unconfirmed), `D` (Declined); plans where the only match is `D` are skipped. Pass `include_declined=True` to keep them.
- **Attribution is plan-level, not song-level.** The Services API has no per-song-item person field: a person is assigned to a *plan* (optionally with a position), not to an individual song. The tool therefore returns every song item of each plan where the person matched. Read it as "songs in services this person played", not "songs this person personally led".
- **Every result is scoped to a scan window.** Plans are collected newest-first (optionally limited to one service type), then walked until `limit` songs are gathered or the window is exhausted.
  - `matched_plans` counts matched plans *inside the scanned window* — not all-time.
  - `scanned_plans` is how many plans were inspected; `matched_plans` may exceed `returned_plans` only if truncation occurred.
  - `scan_complete` is `true` only when the scan ended because plans ran out (or `max_plans` was hit), never when it stopped early because the song limit was reached. A `false` here means a deeper scan exists — raise `limit` or `max_plans`.
  - `limit` therefore also controls scan depth. With sparse attendance you may need a much larger `limit` than the number of songs you actually want.
  - `max_plans` caps plans fetched per service type (default 320) to bound runtime.
- **Requests are paced** to stay under Planning Center's 100-requests-per-20s per-app limit, with backoff on `429`/`5xx`. `requests`, `retries`, and `rate_limited` report what happened; a history call costs several requests per plan.
- Failures are non-fatal where possible: a service type or plan that errors is listed in `skipped` and the rest continues. If no service type could be read at all, the call raises.

## Setup

Requirements: Python 3.12+, a Planning Center account with a **Personal Access Token** (appId + secret).

```bash
git clone <repo-url> ~/ai/pco-mcp
cd ~/ai/pco-mcp
python3 -m venv venv          # or: uv venv venv
./venv/bin/pip install -r requirements.txt
```

Place credentials in your home directory — **outside** the repo:

```json
// ~/.mpco_credentials.json
{
  "myaccount": {
    "appId": "…",
    "appSecret": "pco_pat_…"
  }
}
```

Multiple accounts are separate top-level keys; select one per call with `account=`. See `.env.example`.

Smoke test:

```bash
./venv/bin/python -c "import server; print(server.mcp.name)"
```

### Registering with omp

`~/.omp/agent/mcp.json`:

```json
{
  "mcpServers": {
    "pco": {
      "command": "omp-mcp-supervisor",
      "args": [
        "--",
        "<venv-python>",
        "/path/to/pco-mcp/server.py"
      ],
      "enabled": true
    }
  }
}
```

`<venv-python>` is the interpreter inside the venv the dependencies were installed into (e.g. `/path/to/pco-mcp/venv/bin/python`). The server itself is a plain stdio MCP server, so any MCP client works with the same command.

## Example calls

```python
pco_list_accounts()
pco_list_service_types(account="rs")
pco_list_plans(account="rs", filter="past", per_page=5)
pco_get_plan("https://services.planningcenteronline.com/plans/12345678", account="rs", include_attachments=False)
pco_search_songs("Great Are You Lord", account="rs")
pco_get_song("654321", account="rs")
pco_person_song_history(account="rs", person_name="Alex Morgan", limit=15)
pco_person_song_history(account="rs", person_name="Getz, Henry", limit=50, max_plans=500, include_declined=True)
```

## No secrets in the repo

Credentials live only in `~/.mpco_credentials.json` and are read at runtime; they are never templated into, cached in, or written by the repo. `venv/`, `__pycache__/`, backups, and generated report artifacts are gitignored. Before committing, scan for the real values:

```bash
grep -rInF -e "$(jq -r 'to_entries[0].value.appId' ~/.mpco_credentials.json)" \
            -e 'pco_pat_' --exclude-dir=.git --exclude-dir=venv .
```

Expect only `.env.example`/`README.md` documentation hits for `pco_pat_`, and no hit for a real appId.

## License

Private repository; no license granted. The Planning Center API is used under its own terms.
