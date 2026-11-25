# Advanced configuration and operations

Multi-organization credentials, account routing, manual installation, MCP client
registration, and troubleshooting for **pco-mcp**.

For the tool-by-tool contract, see the [tool reference](../docs/REFERENCE.md).
For the project overview, see the [main README](../README.md).

## Credentials file

Credentials live in `~/.mpco_credentials.json` — **outside** the repository — as
one JSON object per account:

```json
{
  "main": {
    "appId": "…",
    "appSecret": "pco_pat_…",
    "orgName": "Main Campus"
  },
  "north": {
    "appId": "…",
    "appSecret": "pco_pat_…",
    "orgName": "North Campus"
  }
}
```

Each top-level key is an account name. Each entry holds a Planning Center
Personal Access Token (`appId` + `appSecret`). `orgName` is optional and is what
tools report as `org_name`. Multiple organizations are separate top-level keys;
no code change is needed.

Both `orgName` and `org_name` are accepted on read; the server writes camelCase.
Never put real credentials in the repo. See [.env.example](../.env.example) for
the full configuration reference.

## `org_name` precedence

Highest first:

1. `orgName` (or `org_name`) in the credentials entry.
2. `orgNames[account]` in `~/.mpco_config.json`.
3. The account name itself.

`~/.mpco_config.json` holds non-secret preferences only:

```json
{
  "defaultAccount": "north",
  "orgNames": {"north": "North Campus"}
}
```

It is written only by `pco_set_default_account(..., persist=True)`.

## Installation default precedence

The installation default is computed once per server process, then kept as a
snapshot:

1. `PCO_DEFAULT_ACCOUNT` environment variable.
2. `"defaultAccount"` in `~/.mpco_config.json`.
3. First key in `~/.mpco_credentials.json`.
4. `"env"` when credentials come only from `PCO_APP_ID` + `PCO_SECRET` /
   `PCO_APP_SECRET`.
5. The built-in fallback constant `DEFAULT_ACCOUNT_FALLBACK` in
   `pco_accounts.py`.

An explicit default (env or config) that names an unknown account raises instead
of falling back. Editing `~/.mpco_config.json` or the environment does not change
the snapshot of an already-running server process; new chats read the new value.

## Per-chat account selection

**Scope is the chat's stable `lazy-tool serve` process.** omp starts one such
process per chat. lazy-tool may start a fresh `pco` child for every tool call, so
under lazy-tool the selection is stored in a small non-secret per-chat runtime
file keyed to that stable process (PID + start time):

```text
$XDG_RUNTIME_DIR/pco-mcp/chat-<pid>-<start>.json
~/.cache/pco-mcp/chat-<pid>-<start>.json    # when XDG_RUNTIME_DIR is unset
```

The file is mode `0600`, contains only the account name (no credentials), and is
deleted when the selection is cleared. Direct stdio without lazy-tool keeps the
selection in memory.

Two chats or agents running in parallel have different lazy-tool processes and
therefore different runtime files: `pco_use_account` in one cannot change what
another resolves. It never writes `~/.mpco_config.json` or the credentials file.
If a custom client multiplexes several chats through one `lazy-tool` process,
they share that process's scope; pass explicit `account=` when hard isolation
matters.

Precedence for read tool calls, highest first:

| Priority | Source | Set by |
|---|---|---|
| 1 | `explicit` — the `account=` argument (or `"all"`) | per call |
| 2 | `chat` — this chat's selection | `pco_use_account` |
| 3 | `installation` — the default snapshot above | env / `~/.mpco_config.json` / credentials order |

`account="all"` bypasses this and fans out over `list_accounts()`.

Writes never consult priorities 2 or 3 and never accept `"all"`: the `account=`
argument must name one concrete account, so a chat selected for one organization
cannot redirect a write to it.

## Choosing an account for a chat

`pco_use_account(account)` selects an account for this chat only. It validates
the name and usable credentials, returns `pco_account_status()` plus `changed`
and a `message`, and writes only the per-chat runtime file. Call it with no
argument (or an empty string) to clear the selection. `"all"` is rejected.

`pco_set_default_account(account, persist=False)` changes this chat's routing
immediately (`scope: "chat"`). With `persist=True` it also writes
`~/.mpco_config.json` (`scope: "persisted"`, `config_file` set), which changes
the installation default for **new** chats only; running chats keep their current
snapshot and selection. Without `persist`, `config_file` is `None` and no config
is written. The credentials file is never written.

```python
pco_use_account("north")                        # this chat only; no config write
pco_use_account()                               # clear; omitted calls revert
pco_set_default_account("north")                # this chat only; no config write
pco_set_default_account("north", persist=True)  # new chats default to north
```

## Explicit account and `account="all"`

An explicit `account=` always wins over any chat selection. An unknown name
raises; there is no silent fallback to the first account.

`account="all"` ignores the chat selection and queries every configured account
sequentially. Fan-out tools return:

```json
{
  "account": "all",
  "account_source": "all",
  "accounts": ["main", "north"],
  "results": [{"account": "main", "org_name": "Main Campus", "data": "..."}],
  "errors": [{"account": "north", "org_name": "North Campus", "error": "..."}]
}
```

A failing account stays in `errors`; every successful account stays in
`results`, so partial failure keeps the successes. If every account fails, the
call raises a `RuntimeError` aggregating the messages. `pco_get_plan` also sets
top-level `"ambiguous": true` when more than one account has the plan.
`pco_person_upcoming` adds merged `plans`, `count`, and `summary` fields.

## Adding a second organization

1. Add another top-level key to `~/.mpco_credentials.json`:

   ```json
   {
     "main": {
       "appId": "…",
       "appSecret": "pco_pat_…",
       "orgName": "Main Campus"
     },
     "north": {
       "appId": "…",
       "appSecret": "pco_pat_…",
       "orgName": "North Campus"
     }
   }
   ```

2. Use it per call:

   ```python
   pco_list_service_types(account="north")
   pco_list_plans(account="north", filter="future")
   ```

3. Or select it for the current chat, fan out across both, or persist it as the
   default for new chats:

   ```python
   pco_use_account("north")
   pco_list_service_types(account="all")
   pco_set_default_account("north", persist=True)
   ```

`persist=True` writes:

```json
{
  "defaultAccount": "north"
}
```

Without `persist=True` nothing is written and only this chat changes. Running
chats keep their existing snapshot, so a persisted change takes effect on the
next chat.

`orgNames` can also live in that file
(`{"orgNames": {"north": "North Campus"}}`); an `orgName` in the credentials
entry wins over it.

## Environment-only fallback

`PCO_APP_ID` plus `PCO_SECRET` (or `PCO_APP_SECRET`; `PCO_SECRET` is read first)
provide a legacy env-only credential pair, exposed as a synthetic account named
`"env"`.

- When a credentials file exists, its named entries always win and env
  credentials are used only for `account="env"`.
- When no credentials file exists (or it has no accounts), the env pair serves
  any requested account name for reads, so existing single-account setups keep
  working unchanged.
- Writes are stricter: in env-only mode the only configured write name is
  `"env"`; read aliases are not accepted as write targets.
- `PCO_DEFAULT_ACCOUNT` sets the installation default; an explicit default that
  names an unknown account raises instead of falling back.

## Manual installation and update

Requirements: Python 3.12+ and a Planning Center Personal Access Token
(`appId` + secret). The token needs **write permission** for the write tools; a
read-only token previews fine but any `confirm=True` call fails with `403`.

```bash
git clone <repo-url> pco-mcp
cd pco-mcp
python3 -m venv venv          # or: uv venv venv
./venv/bin/pip install -r requirements.txt
```

Update an existing checkout:

```bash
cd pco-mcp
git pull --ff-only
./venv/bin/pip install -r requirements.txt
```

Smoke test:

```bash
./venv/bin/python -c "import server; print(server.mcp.name)"
```

Tests (stdlib `unittest`, no network, temporary credential files):

```bash
./venv/bin/python -m unittest discover -s tests -t . -v
```

## Registering with an MCP client

For omp, edit `~/.omp/agent/mcp.json`:

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

`<venv-python>` is the interpreter inside the venv the dependencies were
installed into. The server itself is a plain stdio MCP server, so any MCP client
works with the same command.

## Troubleshooting and rate limits

- **Start with `pco_account_status()`.** It performs no network calls and shows
  this chat's `effective_account`, `effective_source`, and any
  `routing_warning`.
- **`routing_warning` set.** No accounts are configured, several accounts are
  configured and none is selected, or the saved selection is no longer usable.
  Call `pco_use_account` or pass `account=`.
- **Unknown account.** An explicit name that is not configured raises; there is
  no fallback to the first account.
- **Selection disappeared.** If credentials were edited and the selected account
  vanished, calls that would use it raise `AccountError` telling you to call
  `pco_use_account`. There is no silent fallback.
- **`401`.** The app id or secret is wrong or revoked for that account.
- **`403` on a read.** The token lacks access to that resource.
- **`403` on `confirm=True`.** The token is read-only or lacks write permission;
  reads and previews still work.
- **Rate limits.** Planning Center allows 100 requests per 20 seconds per app.
  Read scans pace requests and back off on `429`/`5xx`; `requests`, `retries`,
  and `rate_limited` report what happened. A person-history call costs several
  requests per plan. On a write `429`, wait and re-preview; writes are never
  retried automatically.
- **`5xx` or timeout on a write.** `outcome_unknown: true` means the write may
  have landed. Check the plan in Planning Center before retrying; `POST` is never
  replayed.
- **`plan_times_error`.** A non-200 plan-times response leaves `plan_times` empty
  but still returns the plan.
- **`scan_complete: false`.** The scan window was truncated or a request failed.
  Raise `limit`/`max_plans` for history, or inspect `skipped`.
- **`ambiguous: true`.** The person name matched several people, or
  `account="all"` found a plan id in several accounts. Narrow the name or pass a
  single `account=`.
- **Config changes appear stale.** The installation default is snapshotted per
  server process; start a new chat/process for a persisted change to apply.

## See also

- [Main README](../README.md)
- [Tool reference](../docs/REFERENCE.md)
- [.env.example](../.env.example) — full configuration reference, not loaded by
  the server.
