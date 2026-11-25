# pco-mcp: an AI assistant for Planning Center Services

**Your Planning Center plans, setlists, and song library, answerable in plain English.**

pco-mcp connects a Planning Center Services organization to any MCP-capable AI app — Claude Desktop, Cursor, Codex, omp, and others. MCP is the standard those apps use to talk to outside tools, so the same server works everywhere. You set it up once with a Planning Center Personal Access Token; after that, you just ask.

## What you can ask

- "What services are coming up at Main Campus next month?"
- "Show me the setlist and keys for this Sunday."
- "What songs has Alex Morgan played, and in what keys?"
- "Is Alex Morgan serving anywhere in the next three weeks?"
- "Find 'Great Are You Lord' in our song library and show me its arrangements."
- "Across all campuses, what plans are scheduled for December?"
- "Add a rehearsal to the Christmas Eve plan from 6:30 to 8:00 PM — preview it first."
- "Move the second service time back 30 minutes — show me the change before you make it."

Answers come from your actual Planning Center data. When a person's name is ambiguous or a plan cannot be found, the server returns candidates or an error instead of guessing.

## Install in one line

macOS / Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/HenryGetz/pco-mcp/main/install.sh | bash
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/HenryGetz/pco-mcp/main/install.ps1 | iex"
```

1. **Run the installer** for your system. It creates the install folder, a Python environment, and the server, then prints the exact Python and `server.py` paths you need below.
2. **Create a Personal Access Token** in Planning Center at <https://api.planningcenteronline.com/oauth/applications> and paste the app ID and secret when the installer asks. The token needs **write** permission if you want the assistant to change plans; read-only is enough for questions. Credentials are saved to `~/.mpco_credentials.json`, outside the repo.
3. **Connect your AI app** with the configuration in the next section, then restart the app completely — quit it, don't just close the window.

## Connect your AI app

pco-mcp is a standard **stdio MCP server**. Add this entry to your app's MCP settings:

```json
{
  "mcpServers": {
    "pco": {
      "command": "<install-dir>/venv/bin/python",
      "args": ["<install-dir>/server.py"]
    }
  }
}
```

Replace `<install-dir>` with the path the installer printed. No token or secret goes in this file — the server reads `~/.mpco_credentials.json` itself. On Windows the interpreter path is `<install-dir>\venv\Scripts\python.exe`.

| App | Where the entry goes |
|---|---|
| **Any MCP client** | Its "add MCP server" or stdio server setting; use the JSON shape above. |
| **Claude Desktop** | `claude_desktop_config.json` — macOS `~/Library/Application Support/Claude/`, Windows `%APPDATA%\Claude\`. |
| **Cursor** | `~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project. |
| **Codex** | `~/.codex/config.toml`, in an `[mcp_servers.pco]` table with `command` and `args`. |
| **omp** | `~/.omp/agent/mcp.json`, under `mcpServers`. |

App-specific paths and formats above are examples; use your app's MCP settings. If your app has an "Add MCP server" screen, prefer it and paste the command and argument paths there.

## What it can do

**Reads** (always safe):

- List service types and upcoming or past plans.
- Open a full plan: item order, songs, arrangements, keys, BPM, meter, notes, attachments, and service or rehearsal times.
- Search the song library and show a song's arrangements and default keys.
- Show a person's song history, with the key each song was in.
- Show a person's upcoming assignments for the next N days, with positions and confirmation status.
- List the Planning Center organizations you have connected.

**Controlled writes** (preview first):

- Create, update, or delete plans.
- Create, update, or delete plan items, including songs and section headers.
- Create, update, or delete plan times (service, rehearsal, or other).

By default, every write starts as a **preview**: the assistant shows the exact request and payload, and nothing changes until you tell it to go ahead. A confirmed call sends exactly one request. The token needs write permission for any confirmed write; a read-only token can still preview.

## Multiple campuses

Each Planning Center organization gets its own account nickname and Personal Access Token. Reads can span every connected account in one tool call — ask "list December plans across all accounts" and the server fans out. Writes always name one account, so a change cannot land on the wrong campus.

The installer creates your first entry. To add a second organization, add another top-level entry to `~/.mpco_credentials.json`:

```json
{
  "main":  { "appId": "…", "appSecret": "pco_pat_…", "orgName": "Main Campus" },
  "north": { "appId": "…", "appSecret": "pco_pat_…", "orgName": "North Campus" }
}
```

The nicknames are yours to choose. Ask "what accounts are connected?" to see the exact names. See [advanced configuration](docs/ADVANCED.md) for adding organizations, choosing a default, and account routing.

## Safety and privacy

- Credentials live only in `~/.mpco_credentials.json`, outside the repository, and are never printed by the tools.
- Reads are `GET`-only; they change nothing.
- Writes are preview-by-default. A confirmed write sends one request and is **never retried automatically**.
- Deletes require an exact guard — a plan title and/or date, an item title, or a plan-time start — and refuse if the current record does not match.
- If a write times out or hits a server error, the outcome may be unknown. Check the plan in Planning Center before trying again so you do not create a duplicate.
- Every write must name one organization explicitly.

## Troubleshooting

- **The AI doesn't see the pco tools.** Restart the app fully after saving its MCP settings, then start a new chat. Some apps also have an enable or approve toggle for each server.
- **The server won't start.** Use the exact Python path the installer printed (`<install-dir>/venv/bin/python`), not your system Python. If the install folder moved, update the path or re-run the installer.
- **A confirmed change fails with 403.** The token is read-only. Create a new token with write permission and replace it in `~/.mpco_credentials.json`.
- **"Account not found."** Ask "list my Planning Center accounts"; the names are the top-level keys in `~/.mpco_credentials.json`. Writes need one of those names, not "all".
- **Where settings and logs live.** Credentials: `~/.mpco_credentials.json`. Non-secret preferences: `~/.mpco_config.json`. The server writes no log file of its own; startup errors and request output appear in your AI app's MCP or server log, or in the terminal that launched it.

## Learn more

- [Tool reference](docs/REFERENCE.md) — every tool, argument, and return shape.
- [Advanced configuration](docs/ADVANCED.md) — multiple organizations, defaults, and routing.
- Developers: after installing, run the test suite with

  ```bash
  cd <install-dir>
  ./venv/bin/python -m unittest discover -s tests -t . -v
  ```

- Source: <https://github.com/HenryGetz/pco-mcp>
- License: no license has been granted yet; the code is all rights reserved. The Planning Center API is used under its own terms.
