# pco-mcp

A [FastMCP](https://github.com/jlowin/fastmcp) stdio server for the **Planning Center Online Services API v2**. It exposes ten tools for reading service types, plans, setlists, songs, arrangements and a person's song history across one or many Planning Center organizations, and is registered with [omp](https://github.com/ompfine/omp) as `pco`. Account routing is per chat: each chat can pick its own account without disturbing any other chat.

Read-only: every tool issues `GET` against `https://api.planningcenteronline.com/services/v2`. Nothing is written back to Planning Center.

## Accounts

Account credentials live in `~/.mpco_credentials.json` as one JSON object per account.
Every tool that reads Planning Center data takes `account`:

- **Omitted** — this chat's selected account if one was set with `pco_use_account`, otherwise the installation default snapshot.
- **A name** — that account only, and it always wins over any chat selection. An unknown name raises; there is no silent fallback to the first account.
- **`"all"`** — fan out over every configured account, sequentially; ignores any chat selection.

### Installation default precedence

The installation default is computed once per server process, then kept as a snapshot:

1. `PCO_DEFAULT_ACCOUNT` environment variable
2. `"defaultAccount"` in `~/.mpco_config.json`
3. first key in `~/.mpco_credentials.json`
4. `"env"` when credentials come only from `PCO_APP_ID` + `PCO_SECRET`/`PCO_APP_SECRET`
5. `"unw"` as the last-resort fallback

An explicit default (env or config) that names an unknown account raises instead of
falling back. Editing `~/.mpco_config.json` or the environment does not change the
snapshot of an already-running server process; new chats read the new value.

## Per-chat account selection

**Scope is the chat's stable `lazy-tool serve` process.** omp starts one such process
per chat. lazy-tool may start a fresh `pco` child for every tool call, so under
lazy-tool the selection is stored in a small non-secret per-chat runtime file keyed to
that stable process (PID + start time):
`$XDG_RUNTIME_DIR/pco-mcp/chat-<pid>-<start>.json`, or
`~/.cache/pco-mcp/chat-<pid>-<start>.json` when `XDG_RUNTIME_DIR` is unset. The file is
mode `0600`, contains only the account name (no credentials), and is deleted when the
selection is cleared. Direct stdio without lazy-tool keeps the selection in memory.

Two chats or agents running in parallel have different lazy-tool processes and
therefore different runtime files: `pco_use_account` in one cannot change what another
resolves. It never writes `~/.mpco_config.json` or the credentials file. If a custom
client multiplexes several chats through one `lazy-tool` process, they share that
process's scope; pass explicit `account=` when hard isolation matters.

Precedence for every tool call, highest first:

| Priority | Source | Set by |
|---|---|---|
| 1 | `explicit` — the `account=` argument (or `"all"`) | per call |
| 2 | `chat` — this chat's selection | `pco_use_account` |
| 3 | `installation` — the default snapshot above | env / `~/.mpco_config.json` / credentials order |

`account="all"` bypasses this and fans out over `list_accounts()`.

Built-in tools:

- `pco_account_status()` — what this chat will use, with no network calls. Returns
  `instance_id` (per pco process; may change per call under lazy-tool), `pid`, `scope`
  (`"chat"` under lazy-tool, `"process"` for direct stdio), `scope_id` (stable
  `lazy-tool` PID:starttime, or `null` for direct stdio), `installation_default`,
  `installation_default_source`, `selected_account`, `effective_account`,
  `effective_source` (`"chat"` or `"installation"`), `routing_warning` (set when no
  accounts are configured, when several accounts are configured and none is selected, or
  when the saved selection is no longer usable), and per-account rows with `account`, `org_name`, `default`, `selected`,
  `effective`, `has_credentials`. `default` uses the installation snapshot (not a fresh
  config read) and `effective` is the selection when set, else that snapshot, so both
  match actual routing.
- `pco_use_account(account)` — selects an account for this chat only. Validates the
  name and usable credentials, then returns `pco_account_status()` plus `changed` and a
  `message`. `pco_use_account()` with no argument (or an empty string) clears the
  selection, which reverts omitted calls to the installation default.
  `"all"` is rejected — it is a per-call fan-out, not a selectable account. Writes only
  the per-chat runtime selection file described above; never the config or credentials
  file.
- `pco_set_default_account(account, persist=False)` — changes this chat's routing
  immediately (`scope: "chat"`). With `persist=True` it also writes
  `~/.mpco_config.json` (`scope: "persisted"`, `config_file` set), which changes the
  installation default for **new** chats only; running chats keep their current
  snapshot and selection. Without `persist`, `config_file` is `None` and no config is
  written (only the per-chat runtime selection). The credentials file is never written.

Every single-account result also carries `account_source` (`"explicit"`, `"chat"`, or
`"installation"`) so you can see which rule selected the account. A fan-out envelope
carries `"account_source": "all"` at the top level, and each envelope row keeps its own
`account` / `org_name` tags.

If the selected account disappears (for example credentials were edited), calls that
would use it raise an `AccountError` telling you to call `pco_use_account`; there is no
silent fallback to the installation default.

For example, in one chat:

```python
pco_account_status()                 # e.g. effective: unw, source installation
pco_use_account("rs")
pco_list_service_types()             # North Campus, account_source: chat
pco_list_service_types(account="unw")  # explicit account always wins
pco_use_account()                    # clear; omitted calls revert to unw
```

### `account="all"` envelope

Fan-out tools return an envelope instead of a single-account result:

```json
{
  "account": "all",
  "account_source": "all",
  "accounts": ["unw", "rs"],
  "results": [
    {"account": "unw", "org_name": "Main Campus", "data": ["..."]},
    {"account": "rs", "org_name": "North Campus", "data": ["..."]}
  ],
  "errors": [
    {"account": "newhope", "org_name": "South Campus", "error": "AccountError: 401 Unauthorized"}
  ]
}
```

`data` is that account's normal single-account result: a list for
`pco_list_service_types`, `pco_list_plans`, and `pco_search_songs`; a dict for
`pco_get_plan`, `pco_get_song`, and `pco_person_song_history`. One account failing does
not discard the others — the failure is listed in `errors` and every successful account
stays in `results`. If every account fails, the call raises a `RuntimeError` aggregating
the messages.

### Tagging

Single-account results keep their existing top-level shape and add `account`,
`org_name`, and `account_source`. For list tools every row is tagged the same way:
`account`, `org_name`, and `account_source`. Every item inside a plan is tagged with
`account` and `org_name`. `account_source` records which rule picked the account
(`"explicit"`, `"chat"`, or `"installation"`). `org_name` comes from `orgName` in the
credentials entry, else `orgNames[account]` in `~/.mpco_config.json`, else the account
name. Both `orgName` and `org_name` are accepted on read; the server writes camelCase.

## Tools

Ten tools. Every tool with an `account` parameter resolves it the same way: omitted
uses this chat's `pco_use_account` selection, else the installation default snapshot;
a name always wins over the chat selection; `"all"` fans out over every configured
account and ignores the selection. Results carry `account_source`.

### `pco_list_accounts(detailed=False) -> list`
Account names configured in `~/.mpco_credentials.json`. When no credentials file
accounts exist and `PCO_APP_ID` + `PCO_SECRET` are set, the synthetic `"env"` account is
listed instead. Useful as a first call to discover valid `account` values.

With `detailed=True`, returns
`[{"account", "org_name", "default", "selected", "effective", "has_credentials"}]`.
`default` marks the installation-default snapshot account, `selected` marks this chat's
`pco_use_account` selection (at most one row), and `effective` marks the account omitted
calls currently resolve to; `has_credentials` reports whether a usable pair was found.

### `pco_account_status() -> dict`
What this chat will do, with no network calls: `instance_id` (unique per pco process;
under lazy-tool a fresh process may serve each call), `pid`, `scope` (`"chat"` under
lazy-tool, `"process"` for direct stdio), `scope_id` (stable lazy-tool PID:starttime, or
`null`), `installation_default`,
`installation_default_source` (`"env:PCO_DEFAULT_ACCOUNT"`, `"config"`, `"first-key"`,
`"env-credentials"`, or `"builtin"`), `selected_account`, `effective_account`,
`effective_source` (`"chat"` or `"installation"`), `routing_warning` (a string when no
accounts are configured, when several accounts are configured and none is selected, or
when the saved selection is no longer usable), and `accounts` rows with `account`, `org_name`, `default`, `selected`,
`effective`, `has_credentials`. `installation_default` (and its source) is `null` only
when the installation default is unresolvable and a chat selection is set.

### `pco_use_account(account=None) -> dict`
Select the account for **this chat only**. Returns
`pco_account_status()` plus `changed` and a `message`. Omitting the argument — or passing
an empty string — clears the selection, reverting omitted calls to the installation
default. Rejects `"all"` (fan-out is per call via `account="all"`). Validates that the
name resolves and has usable credentials. Under lazy-tool it writes only the small
non-secret per-chat runtime file described above, never the config or credentials file;
other chats are unaffected. An explicit `account=` on any tool always overrides this
selection.

### `pco_set_default_account(account, persist=False) -> dict`
Change the account used when a tool call omits `account`, effective immediately for this
chat. Returns
`{"default_account", "scope", "config_file", "accounts", "status"}` where `scope` is
`"chat"` (`persist=False`) or `"persisted"` (`persist=True`). Rejects `"all"`; validates
that the account is configured and has usable credentials.

- `persist=False` (default) changes only this chat; `config_file` is `None` and **no
  config file is written** (only the per-chat runtime selection).
- `persist=True` also writes `~/.mpco_config.json` (`defaultAccount`) so **new** chats
  start from this account; `config_file` is the written path. Running chats keep their
  current snapshot and selection.

The credentials file is never written.

### `pco_list_service_types(account=None) -> list[dict]`
Service types in the account (e.g. Sunday Morning, Youth, Chapels). Returns `id`, `name`,
`frequency`, plus `account` and `org_name` tags.

### `pco_list_plans(service_type_id=None, filter="future", per_page=10, account=None) -> list[dict]`
Plans for one service type, or across every service type when `service_type_id` is
omitted. `filter` is `"future"`, `"past"`, or `"all"`. Returns `id`, `service_type_id`,
`service_type_name`, `title`, `dates` (human-readable), `sort_date` (ISO, for sorting),
plus `account` and `org_name` tags.

### `pco_get_plan(plan_id_or_url, account=None, include_attachments=True) -> dict`
Full plan by numeric ID or by pasted Planning Center URL. Locates the owning service type, then returns plan metadata plus `items` sorted by `sequence`. Song items carry `song_id`, `song_title`, `author`, `ccli_number`, `arrangement_id`, `arrangement_name`, `key`, `bpm`, `meter`, `arrangement_notes`; set `include_attachments=False` to skip the per-song arrangement attachment lookups (this is the expensive part — one request per song item). Every item is tagged with `account` and `org_name`; the top level carries both too.

`plan_times` lists the plan's times sorted by `starts_at` with `id`, `name`, `time_type` (`service`/`rehearsal`/`other`), `starts_at`, and `ends_at`. `counts` exposes `service_time_count`, `rehearsal_time_count`, `other_time_count`, `plan_people_count`, `items_count`, `total_length`, and `multi_day`. A non-200 plan-times response leaves `plan_times` empty and adds `plan_times_error`; the plan itself still returns.

With `account="all"`, the fan-out envelope includes every account where the plan id resolves, and sets top-level `"ambiguous": true` when more than one account has it.

### `pco_search_songs(query, per_page=10, account=None) -> list[dict]`
Library search by title/keyword. Falls back to a plain title-ordered listing when the filtered query returns nothing. Returns `id`, `title`, `author`, `ccli_number`, plus `account` and `org_name` tags. The query is sent as a request parameter, never interpolated into the path.

### `pco_get_song(song_id, account=None) -> dict`
One song with its arrangements (`name`, `bpm`, `meter`, `default_key`, `notes`) plus copyright, CCLI number, `account`, and `org_name`.

### `pco_person_song_history(account=None, person_name="", service_type_id=None, limit=15, max_plans=320, include_declined=False) -> dict`
The songs a person was scheduled for, **newest first**, with the key each song was in.

`person_name` is required and matched case- and whitespace-insensitively; `"Last, First"` is accepted as well as `"First Last"`. If several people match exactly, the tool returns `ambiguous: true` with `candidates` instead of guessing; if nobody matches exactly it returns `candidates` (near matches) and a note.

Returned shape:

```json
{
  "account": "unw",
  "org_name": "Main Campus",
  "account_source": "explicit",
  "person_name": "Alex Morgan",
  "person_id": "1234567",
  "matched_plans": 3,
  "returned_plans": 3,
  "scan_complete": false,
  "scanned_plans": 7,
  "count": 15,
  "plans": [
    {
      "account": "unw", "org_name": "Main Campus",
      "plan_id": "...", "date": "2026-11-16", "dates": "November 16, 2026",
      "title": "Sunday Morning", "service_type_id": "...", "service_type_name": "Sunday Morning",
      "positions": ["Acoustic Guitar"], "statuses": ["C"],
      "assignments": [{"person_id": "...", "name": "...", "position": "...", "status": "C",
                       "status_label": "Confirmed", "decline_reason": null,
                       "status_updated_at": "2026-11-14T18:00:00Z"}],
      "songs": [{"title": "...", "key": "G", "sequence": 2, "...": "..."}]
    }
  ],
  "skipped": [],
  "requests": 42,
  "retries": 0,
  "rate_limited": 0
}
```

Every plan entry is tagged with `account` and `org_name`; the top level carries both too. Each assignment includes `decline_reason` and `status_updated_at` in addition to the existing fields.

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
    "appSecret": "pco_pat_…",
    "orgName": "My Church"
  }
}
```

`orgName` is optional; when present it is what tools report as `org_name`. Multiple
accounts are separate top-level keys; select one per call with `account=`. See
`.env.example`.

### Adding a second org

Add another top-level key — no code change is needed:

```json
// ~/.mpco_credentials.json
{
  "unw": {
    "appId": "…",
    "appSecret": "pco_pat_…",
    "orgName": "Main Campus"
  },
  "rs": {
    "appId": "…",
    "appSecret": "pco_pat_…",
    "orgName": "North Campus"
  }
}
```

Then either pass `account="rs"` per call, fan out with `account="all"`, select it for the
current chat, or persist it as the installation default for new chats:

```python
pco_use_account("rs")                        # this chat only; no config write
pco_set_default_account("rs", persist=True)  # ~/.mpco_config.json, new chats only
```

`persist=True` writes `~/.mpco_config.json`:

```json
{
  "defaultAccount": "rs"
}
```

Without `persist=True` nothing is written and only this chat changes. Running chats keep
their existing snapshot, so a persisted change takes effect on the next chat.

`orgNames` can also live in that file (`{"orgNames": {"rs": "North Campus"}}`); an
`orgName` in the credentials entry wins over it.

Smoke test:

```bash
./venv/bin/python -c "import server; print(server.mcp.name)"
```

Run the tests (stdlib `unittest`, no network, temporary credential files):

```bash
./venv/bin/python -m unittest discover -s tests -t . -v
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
pco_account_status()                             # what this chat resolves, no network
pco_list_accounts()
pco_list_accounts(detailed=True)
pco_use_account("rs")                            # this chat -> rs; no config write
pco_use_account()                                # clear -> installation default
pco_set_default_account("rs")                    # this chat only; no config write
pco_set_default_account("rs", persist=True)      # new chats default to rs
pco_list_service_types()                         # chat selection or installation default
pco_list_service_types(account="unw")            # explicit always wins
pco_list_service_types(account="rs")
pco_list_plans(account="rs", filter="past", per_page=5)
pco_list_plans(account="all", filter="future")   # fan-out envelope
pco_get_plan("https://services.planningcenteronline.com/plans/12345678", account="rs", include_attachments=False)
pco_search_songs("Great Are You Lord", account="rs")
pco_get_song("654321", account="rs")
pco_person_song_history(account="rs", person_name="Alex Morgan", limit=15)
pco_person_song_history(account="rs", person_name="Getz, Henry", limit=50, max_plans=500, include_declined=True)
```

## No secrets in the repo

Credentials live only in `~/.mpco_credentials.json` and are read at runtime; they are never templated into, cached in, or written by the repo. `~/.mpco_config.json` holds non-secret preferences (`defaultAccount`, `orgNames`) and is only written by `pco_set_default_account(..., persist=True)`; chat selection via `pco_use_account` or `pco_set_default_account` without `persist` writes only a small non-secret per-chat runtime file (`$XDG_RUNTIME_DIR/pco-mcp/` or `~/.cache/pco-mcp/`). `venv/`, `__pycache__/`, backups, and generated report artifacts are gitignored. Before committing, scan for the real values:

```bash
grep -rInF -e "$(jq -r 'to_entries[0].value.appId' ~/.mpco_credentials.json)" \
            -e 'pco_pat_' --exclude-dir=.git --exclude-dir=venv .
```

Expect only `.env.example`/`README.md` documentation hits for `pco_pat_`, and no hit for a real appId.

## License

Private repository; no license granted. The Planning Center API is used under its own terms.
