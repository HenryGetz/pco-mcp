# pco-mcp tool reference

Complete reference for the twenty tools exposed by **pco-mcp**: eleven read and account
tools for service types, plans, setlists, songs, arrangements, and a person's schedule,
plus nine controlled-write tools for plans, plan items, and plan times.

For installation, credentials, and per-chat routing, see the [main README](../README.md)
and [advanced configuration](ADVANCED.md). Reads are `GET`-only. Writes are
preview-by-default, explicit-account-only, and never retried automatically.

## Account resolution for reads

Read tools accept `account`:

- **Omitted** — this chat's selected account if one was set with `pco_use_account`,
  otherwise the installation default snapshot.
- **A name** — that account only, and it always wins over any chat selection. An unknown
  name raises; there is no silent fallback to the first account.
- **`"all"`** — fan out over every configured account, sequentially; ignores any chat
  selection.

Every single-account result carries `account_source`: `"explicit"`, `"chat"`, or
`"installation"`. A fan-out envelope carries `"account_source": "all"` at the top level,
and each envelope row keeps its own `account` / `org_name` tags. Write tools do **not**
use this resolution; see [Write tools](#write-tools).

## `account="all"` envelope

```json
{
  "account": "all",
  "account_source": "all",
  "accounts": ["main", "north"],
  "results": [
    {"account": "main", "org_name": "Main Campus", "data": ["..."]},
    {"account": "north", "org_name": "North Campus", "data": ["..."]}
  ],
  "errors": [
    {"account": "north", "org_name": "North Campus", "error": "AccountError: 401 Unauthorized"}
  ]
}
```

`data` is that account's normal single-account result: a list for
`pco_list_service_types`, `pco_list_plans`, and `pco_search_songs`; a dict for
`pco_get_plan`, `pco_get_song`, `pco_person_song_history`, and `pco_person_upcoming`.
One account failing does not discard the others — the failure is listed in `errors` and
every successful account stays in `results`. If every account fails, the call raises a
`RuntimeError` aggregating the messages.

`pco_get_plan` includes every account where the plan id resolves and sets top-level
`"ambiguous": true` when more than one account has it. `pco_person_upcoming` adds merged
top-level `plans` (flattened, sorted by date), `count`, and `summary`
(`{accounts_queried, accounts_succeeded, accounts_failed, matched_plans, assignments}`).

## Tagging

Single-account results keep their existing top-level shape and add `account`, `org_name`,
and `account_source`. For list tools every row is tagged the same way; every item inside a
plan is tagged with `account` and `org_name`. `org_name` comes from `orgName` in the
credentials entry, else `orgNames[account]` in `~/.mpco_config.json`, else the account
name. Both `orgName` and `org_name` are accepted on read; the server writes camelCase.
Write results and previews always carry `account_source: "explicit"`.

## Read and account tools

### `pco_list_accounts(detailed=False) -> list`

Account names configured in `~/.mpco_credentials.json`. When no credentials-file accounts
exist and `PCO_APP_ID` + `PCO_SECRET` are set, the synthetic `"env"` account is listed
instead. Useful as a first call to discover valid `account` values.

With `detailed=True`, returns
`[{"account", "org_name", "default", "selected", "effective", "has_credentials"}]`.
`default` marks the installation-default snapshot account, `selected` marks this chat's
`pco_use_account` selection (at most one row), and `effective` marks the account omitted
calls currently resolve to. `has_credentials` reports whether a usable pair was found.

### `pco_account_status() -> dict`

What this chat will do, with no network calls: `instance_id` (unique per pco process;
under lazy-tool a fresh process may serve each call), `pid`, `scope` (`"chat"` under
lazy-tool, `"process"` for direct stdio), `scope_id` (stable lazy-tool PID:starttime, or
`null`), `installation_default`, `installation_default_source` (`"env:PCO_DEFAULT_ACCOUNT"`,
`"config"`, `"first-key"`, `"env-credentials"`, or `"builtin"`), `selected_account`,
`effective_account`, `effective_source` (`"chat"` or `"installation"`), and
`routing_warning` (a string when no accounts are configured, when several accounts are
configured and none is selected, or when the saved selection is no longer usable).
`accounts` rows carry `account`, `org_name`, `default`, `selected`, `effective`, and
`has_credentials`. `installation_default` (and its source) is `null` only when the
installation default is unresolvable and a chat selection is set.

### `pco_use_account(account=None) -> dict`

Select the account for **this chat only**. Returns `pco_account_status()` plus `changed`
and a `message`. Omitting the argument — or passing an empty string — clears the
selection, reverting omitted calls to the installation default. Rejects `"all"` (fan-out
is per call via `account="all"`). Validates that the name resolves and has usable
credentials. Under lazy-tool it writes only the small non-secret per-chat runtime file,
never the config or credentials file; other chats are unaffected. An explicit `account=`
on any tool always overrides this selection.

### `pco_set_default_account(account, persist=False) -> dict`

Change the account used when a tool call omits `account`, effective immediately for this
chat. Returns `{"default_account", "scope", "config_file", "accounts", "status"}` where
`scope` is `"chat"` (`persist=False`) or `"persisted"` (`persist=True`). Rejects `"all"`;
validates that the account is configured and has usable credentials.

- `persist=False` (default) changes only this chat; `config_file` is `None` and **no config
  file is written** (only the per-chat runtime selection).
- `persist=True` also writes `~/.mpco_config.json` (`defaultAccount`) so **new** chats
  start from this account; `config_file` is the written path. Running chats keep their
  current snapshot and selection.

The credentials file is never written.

### `pco_list_service_types(account=None) -> list[dict]`

Service types in the account (for example Sunday Morning, Youth, Midweek). Returns `id`,
`name`, `frequency`, plus `account` and `org_name` tags.

### `pco_list_plans(service_type_id=None, filter="future", per_page=10, account=None) -> list[dict]`

Plans for one service type, or across every service type when `service_type_id` is
omitted. `filter` is `"future"`, `"past"`, or `"all"`. Returns `id`, `service_type_id`,
`service_type_name`, `title`, `dates` (human-readable), `sort_date` (ISO, for sorting),
plus `account` and `org_name` tags.

### `pco_get_plan(plan_id_or_url, account=None, include_attachments=True) -> dict`

Full plan by numeric ID or by pasted Planning Center URL. Locates the owning service type,
then returns plan metadata plus `items` sorted by `sequence`. Every item includes its
numeric `id` for the plan-item write tools. Song items carry `song_id`, `song_title`,
`author`, `ccli_number`, `arrangement_id`, `arrangement_name`, `key`, `bpm`, `meter`, and
`arrangement_notes`. Set `include_attachments=False` to skip the per-song arrangement
attachment lookups (the expensive part — one request per song item). Every item is tagged
with `account` and `org_name`; the top level carries both too.

`plan_times` lists the plan's times sorted by `starts_at` with `id`, `name`, `time_type`
(`service`/`rehearsal`/`other`), `starts_at`, and `ends_at`. `counts` exposes
`service_time_count`, `rehearsal_time_count`, `other_time_count`, `plan_people_count`,
`items_count`, `total_length`, and `multi_day`. A non-200 plan-times response leaves
`plan_times` empty and adds `plan_times_error`; the plan itself still returns.

### `pco_search_songs(query, per_page=10, account=None) -> list[dict]`

Library search by title/keyword. Falls back to a plain title-ordered listing when the
filtered query returns nothing. Returns `id`, `title`, `author`, `ccli_number`, plus
`account` and `org_name` tags. The query is sent as a request parameter, never
interpolated into the path.

### `pco_get_song(song_id, account=None) -> dict`

One song with its arrangements (`name`, `bpm`, `meter`, `default_key`, `notes`) plus
copyright, CCLI number, `account`, and `org_name`.

### `pco_person_song_history(account=None, person_name="", service_type_id=None, limit=15, max_plans=320, include_declined=False) -> dict`

The songs a person was scheduled for, **newest first**, with the key each song was in.

`person_name` is required and matched case- and whitespace-insensitively; `"Last, First"`
is accepted as well as `"First Last"`. If several people match exactly, the tool returns
`ambiguous: true` with `candidates` instead of guessing; if nobody matches exactly it
returns `candidates` (near matches) and a note.

```json
{
  "account": "main", "org_name": "Main Campus", "account_source": "explicit",
  "person_name": "Alex Morgan", "person_id": "1234567",
  "matched_plans": 3, "returned_plans": 3, "scan_complete": false,
  "scanned_plans": 7, "count": 15,
  "plans": [{
    "account": "main", "org_name": "Main Campus",
    "plan_id": "...", "date": "2026-11-16", "dates": "November 16, 2026",
    "title": "Sunday Morning", "service_type_id": "...",
    "service_type_name": "Sunday Morning",
    "positions": ["Acoustic Guitar"], "statuses": ["C"],
    "assignments": [{"person_id": "...", "name": "...", "position": "...",
                     "status": "C", "status_label": "Confirmed",
                     "decline_reason": null, "status_updated_at": "..."}],
    "songs": [{"title": "...", "key": "G", "sequence": 2, "...": "..."}]
  }],
  "skipped": [], "requests": 42, "retries": 0, "rate_limited": 0
}
```

Every plan entry is tagged with `account` and `org_name`; the top level carries both too.
Each assignment includes `decline_reason` and `status_updated_at`.

Semantics worth knowing:

- **`limit` counts songs, not plans.** `count` is the number of song entries returned;
  each plan's `songs` list is truncated to what remains of the limit.
- **Declined assignments are excluded by default.** Team-member status is `C` (Confirmed),
  `U` (Unconfirmed), `D` (Declined); plans where the only match is `D` are skipped. Pass
  `include_declined=True` to keep them.
- **Attribution is plan-level, not song-level.** The Services API has no per-song-item
  person field: a person is assigned to a *plan* (optionally with a position), not to an
  individual song. The tool returns every song item of each plan where the person
  matched. Read it as "songs in services this person played", not "songs this person
  personally led".
- **Every result is scoped to a scan window.** Plans are collected newest-first
  (optionally limited to one service type), then walked until `limit` songs are gathered
  or the window is exhausted. `matched_plans` counts matched plans *inside the scanned
  window* — not all-time. `scanned_plans` is how many plans were inspected;
  `matched_plans` may exceed `returned_plans` only if truncation occurred.
  `scan_complete` is `true` only when the scan ended because plans ran out (or `max_plans`
  was hit), never when it stopped early because the song limit was reached; `false` means
  a deeper scan exists — raise `limit` or `max_plans`. `limit` therefore also controls
  scan depth, and `max_plans` caps plans fetched per service type (default 320).
- **Requests are paced** to stay under Planning Center's 100-requests-per-20s per-app
  limit, with backoff on `429`/`5xx`. `requests`, `retries`, and `rate_limited` report
  what happened; a history call costs several requests per plan.
- **Failures are non-fatal where possible.** A service type or plan that errors is listed
  in `skipped` and the rest continues. If no service type could be read at all, the call
  raises.

### `pco_person_upcoming(person_name="", days=21, account=None, service_type_id=None, include_declined=False, max_plans=80) -> dict`

The plan assignments on a person's calendar for the upcoming window, **earliest first**.
This is the forward-looking counterpart to `pco_person_song_history`: it returns plans,
plan times, positions, and statuses, not songs.

`person_name` is required and matched exactly like `pco_person_song_history` (case- and
whitespace-insensitive; `"Last, First"` accepted). Several exact matches return
`ambiguous: true` with `candidates` instead of guessing; no exact match returns
`candidates` (near matches) and a note. No match is not an error.

The window is **today through today + `days`, inclusive**, evaluated per account. The scan
covers future plans, optionally restricted to one `service_type_id`, and matches the
person against each plan's `team_members`.

Returned fields for one account: `account`, `org_name`, `account_source`, `person_name`,
`person_id`, `ambiguous`, `candidates`, `window` (the resolved inclusive range and day
count), `matched_plans`, `returned_plans`, `assignments_count`, `scanned_plans`,
`scan_complete`, `plans`, `skipped`, `requests`, `retries`, and `rate_limited`.

Each plan entry carries `plan_id`, `date`, `dates`, `title`, `service_type_id`,
`service_type_name`, `plan_times`, `starts_at` (the service start when a service
`PlanTime` exists, else the first plan time with a timestamp), `positions`, `statuses`,
`assignments`, and the `account` / `org_name` tags. Assignment objects use the same fields
as `pco_person_song_history` (`person_id`, `name`, `position`, `status`, `status_label`,
`decline_reason`, `status_updated_at`).

Semantics worth knowing:

- **Declined assignments are excluded by default.** Status `D` is dropped; pass
  `include_declined=True` to keep it. A plan is only returned when the person has at least
  one surviving assignment.
- **`plan_times` is the full schedule for the plan**, sorted by `starts_at`, so a plan with
  several services or rehearsals is not reduced to a single timestamp.
- **`matched_plans`, `scanned_plans`, and `scan_complete` describe the scan.** `max_plans`
  caps plans scanned (default 80) to bound runtime; `scan_complete` reports whether the
  window was fully covered without `max_plans` truncation or request failures. `skipped`
  lists plans or service types that errored without aborting the rest.
- **Requests are paced** as in `pco_person_song_history`; the same request counters report
  what happened.

## Write tools

Nine tools create, update, and delete plans, plan items, and plan times. All of them share
one gate.

- **A concrete account is mandatory.** `account` has no default and must name a configured
  account. `account="all"`, an empty account, the `pco_use_account` chat selection, and the
  installation default are all rejected for writes; there is no fallback. Every write
  response carries `account`, `org_name`, and `account_source: "explicit"`. In a legacy
  env-only setup the only configured write name is the synthetic `env` account; read-side
  aliases are not accepted as write targets.
- **Blank optional strings are omitted, not cleared.** A blank
  `series_title`/`description`/`name` is treated as not supplied; there is no clear-field
  sentinel.
- **Preview by default.** `confirm=False` (the default) runs read-only preflight `GET`s,
  then returns the exact planned request and writes nothing: `mode: "preview"`,
  `would_write: false`, `confirm_required: true`, `account`, `org_name`,
  `account_source: "explicit"`, `operation`, `method`, `path`, `payload`, `preflight` (the
  current target summary), and a `message`.
- **Confirm mutates once.** `confirm=True` preflights again and sends exactly one
  `POST`/`PATCH`/`DELETE`. Success returns `mode: "write"`, `written: true`, `account`,
  `org_name`, `account_source: "explicit"`, `operation`, `method`, `path`, `status_code`,
  `request_id` (PCO's `x-request-id` or `x-pco-api-request-id` when present), `resource`
  (response `data`, or `null`), `deleted` (`true` for a `DELETE`/204), and `message`.
- **Guards are checked before mutating.** `pco_delete_plan` requires `expected_title`,
  `expected_sort_date`, or both — plan titles can be `null`, so `expected_sort_date` alone
  is valid. `expected_sort_date` is normally the plan's date-only `YYYY-MM-DD` value; a
  full ISO-8601 instant is also accepted and compared as an instant.
  `pco_delete_plan_item` requires `expected_title`; `pco_delete_plan_time` requires
  `expected_starts_at`. Each supplied value must match exactly; a mismatch raises before
  any write. Updates accept optional `expected_title`/`expected_sort_date` (plans),
  `expected_title` (plan items), or `expected_starts_at` (plan times) guards.
- **No automatic retry.** `429`, `5xx`, timeouts, and other transport errors are never
  retried, and a `POST` is never replayed. A `429` is retryable by the caller: wait, then
  re-preview. `5xx` and transport failures (including timeouts) set `outcome_unknown: true`:
  the write may have landed, so check the plan in Planning Center before retrying. Other
  `4xx` responses raise `WriteError` with PCO's status and detail; `403` calls out that the
  token lacks write permission.
- **Inputs are strict.** IDs are numeric PCO ids (no URLs or path fragments). Timestamps
  are ISO-8601 with an explicit timezone, and `ends_at` must be later than `starts_at`.
  `time_type` is `service`, `rehearsal`, or `other`; `service_position` is `pre`, `during`,
  or `post`. Updates require at least one attribute to change.
- **Token permissions.** The Personal Access Token needs write permission for any
  `confirm=True` call. A read-only token still previews fine but fails the write with
  `403`.

### Plans

```python
pco_create_plan(account, service_type_id, title, public=False,
                series_title=None, series_id=None, confirm=False)
pco_update_plan(account, service_type_id, plan_id, title=None, public=None,
                series_title=None, series_id=None, reminders_disabled=None,
                expected_title=None, expected_sort_date=None, confirm=False)
pco_delete_plan(account, service_type_id, plan_id, expected_title=None,
                expected_sort_date=None, confirm=False)
```

Create preflights the service type, then creates the plan. `title` is required; `public`
controls public visibility. Update patches only the supplied attributes and requires at
least one; its guards are optional exact matches. Delete requires at least one exact guard;
both are compared when both are supplied. `expected_sort_date` alone works for plans with
a `null` title.

### Plan items

```python
pco_create_plan_item(account, service_type_id, plan_id, title, item_type="item",
                     song_id=None, arrangement_id=None, key_id=None, length=None,
                     description=None, service_position="during", sequence=None,
                     confirm=False)
pco_update_plan_item(account, service_type_id, plan_id, item_id, title=None,
                     description=None, length=None, service_position=None,
                     sequence=None, expected_title=None, confirm=False)
pco_delete_plan_item(account, service_type_id, plan_id, item_id,
                     expected_title, confirm=False)
```

`item_type` is `item`, `header`, or `song`; a `song_id` with `item_type="item"` (the
default) is treated as a song. A header is sent with `item_type="header"`; regular items
and songs omit `item_type`. A song requires `song_id`, and `arrangement_id`/`key_id` are
only valid with it; the song is preflighted against `/songs/{song_id}` under the same
account. Update patches at least one attribute; `expected_title` is an optional exact
guard. Delete requires an exact `expected_title`.

### Plan times

```python
pco_create_plan_time(account, service_type_id, plan_id, starts_at, ends_at,
                     time_type="service", name=None, team_reminders=None,
                     confirm=False)
pco_update_plan_time(account, service_type_id, plan_id, plan_time_id,
                     starts_at=None, ends_at=None, time_type=None, name=None,
                     team_reminders=None, expected_starts_at=None, confirm=False)
pco_delete_plan_time(account, service_type_id, plan_id, plan_time_id,
                     expected_starts_at, confirm=False)
```

Create uses timezone-bearing ISO timestamps; `ends_at` must be later than `starts_at`.
`team_reminders` maps numeric team ids to integers `0`–`7`. Update patches at least one
attribute; the plan-time collection is preflighted under the same account and the id must
exist; `expected_starts_at` is an optional exact instant guard. Delete requires an exact
`expected_starts_at` and preflights that the id exists.

### Preview, then confirm

A preview is not a token and is not replayed: the `confirm=True` call preflights again and
sends the current values, so run the same call twice.

```python
pco_create_plan(account="north", service_type_id="123456", title="Christmas Eve")
# -> {"mode": "preview", "would_write": false, "confirm_required": true,
#     "method": "POST", "path": "/service_types/123456/plans",
#     "payload": {"data": {"type": "Plan", "attributes": {...}}}, "preflight": {...}}
created = pco_create_plan(account="north", service_type_id="123456",
                          title="Christmas Eve", confirm=True)
plan_id = created["resource"]["id"]

pco_create_plan_item(account="north", service_type_id="123456", plan_id=plan_id,
                     title="Great Are You Lord", song_id="654321", key_id="1")
pco_create_plan_item(account="north", service_type_id="123456", plan_id=plan_id,
                     title="Great Are You Lord", song_id="654321", key_id="1",
                     confirm=True)

pco_create_plan_time(account="north", service_type_id="123456", plan_id=plan_id,
                     starts_at="2026-12-24T22:00:00Z", ends_at="2026-12-24T23:30:00Z")
pco_create_plan_time(account="north", service_type_id="123456", plan_id=plan_id,
                     starts_at="2026-12-24T22:00:00Z", ends_at="2026-12-24T23:30:00Z",
                     confirm=True)

pco_delete_plan(account="north", service_type_id="123456", plan_id=plan_id,
                expected_sort_date="2026-12-24")
pco_delete_plan(account="north", service_type_id="123456", plan_id=plan_id,
                expected_sort_date="2026-12-24", confirm=True)
```

## Write safety

`pco_writes.py` is the only module allowed to issue a mutating HTTP verb (`POST`, `PATCH`,
`DELETE`). `server.py` and `pco_accounts.py` stay free of
`post`/`put`/`patch`/`delete`/`request`/`send`; AST-based tests enforce both facts.

Write paths are allowlisted, with numeric ids only:

```text
POST   /service_types/{st}/plans
PATCH  /service_types/{st}/plans/{plan}
DELETE /service_types/{st}/plans/{plan}
POST   /service_types/{st}/plans/{plan}/items
PATCH  /service_types/{st}/plans/{plan}/items/{item}
DELETE /service_types/{st}/plans/{plan}/items/{item}
POST   /service_types/{st}/plans/{plan}/plan_times
PATCH  /service_types/{st}/plan_times/{time}
DELETE /service_types/{st}/plan_times/{time}
```

The write target comes only from the explicit `account=` argument. Chat selection and the
installation default are never consulted, and `"all"` is rejected, so a chat set to one
organization cannot redirect a write to it. Preflight reads use the same explicit account
and fail closed on `403`/`404`, naming that account.

`confirm=False` is always safe: it returns the exact planned request and performs no
mutation. `confirm=True` performs one request with no retry, so a write cannot be silently
duplicated. Results are tagged per organization (`account`, `org_name`,
`account_source: "explicit"`), including previews.

## Environment-only credentials

`PCO_APP_ID` plus `PCO_SECRET` (or `PCO_APP_SECRET`; `PCO_SECRET` is read first) provide a
legacy env-only credential pair, exposed as a synthetic account named `"env"`.

- When a credentials file exists, its named entries always win and env credentials are used
  only for `account="env"`.
- When no credentials file exists (or it has no accounts), the env pair serves any
  requested account name for reads, so existing single-account setups keep working
  unchanged.
- Writes are stricter: in env-only mode the only configured write name is `"env"`; read
  aliases are not accepted as write targets.
- `PCO_DEFAULT_ACCOUNT` sets the installation default. An explicit default that names an
  unknown account raises instead of falling back.

Credentials are read at runtime and never templated into, cached in, or written by the
repo. See the [main README](../README.md) and
[advanced configuration](ADVANCED.md#environment-only-fallback) for setup.

## Developer checks

Smoke test:

```bash
./venv/bin/python -c "import server; print(server.mcp.name)"
```

Run the tests (stdlib `unittest`, no network, temporary credential files):

```bash
./venv/bin/python -m unittest discover -s tests -t . -v
```

The Planning Center API is used under its own terms.
