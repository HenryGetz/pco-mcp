# pco-mcp

**An AI secretary for Planning Center Services.**

pco-mcp connects your Planning Center plans, setlists, songs, and schedule to an AI app that supports MCP. Ask in plain English — no code.

Works with Claude Desktop, Claude Code, Cursor, Codex, omp, and any other MCP-capable client.

## Install

Copy this prompt into your coding agent:

```text
Install and configure pco-mcp for me from https://github.com/HenryGetz/pco-mcp.

1. Clone or update the repo in a sensible local folder.
2. Create a Python 3.12+ virtual environment, install requirements.txt, and run
   venv/bin/python -c "import server; print(server.mcp.name)" (on Windows, use
   venv\Scripts\python.exe).
3. Ask me for an account nickname (default: main), an organization display name,
   and my Planning Center app ID and app secret. I create Personal Access Tokens at
   https://api.planningcenteronline.com/oauth/applications. Write them to
   ~/.mpco_credentials.json as
   {"<nickname>": {"appId": "...", "appSecret": "...", "orgName": "..."}}
   with mode 600, preserve any accounts already in that file, and never print,
   echo, commit, or put the secret in the repo.
4. Detect the AI app I am using (Claude Desktop, Claude Code, Cursor, Codex, omp,
   or another MCP client) and add a stdio MCP server named pco using the absolute
   venv Python and server.py paths. If you cannot safely edit its config, print
   the exact config and tell me where to paste it.
5. Tell me to restart the app, then verify with pco_list_accounts and
   pco_list_service_types. Report the install path, account names, and exact
   restart step.
6. Do not create, edit, or delete anything in Planning Center during setup. Reads only.

If a step fails, stop and tell me exactly what is missing.
```

The agent handles the Python environment, your MCP client config, and the Planning Center token. If it cannot edit your app's config, it will print the exact JSON/TOML for you.

A read-only token is enough for questions. Add write permission if you want the assistant to change plans and service times. The Planning Center API is used under its own terms.

## Try asking

- "What services are coming up at Main Campus?"
- "Show me the setlist and keys for Sunday."
- "What songs has Alex Morgan played, and in what keys?"
- "Is Alex Morgan serving in the next three weeks?"
- "Add a rehearsal to the Christmas Eve plan at 6:30 PM — preview it first."
- "Across all campuses, what plans are scheduled for December?"

## What it does

- Reads service types, plans, setlists, songs, arrangements, keys, people's song history, and upcoming assignments.
- Reads across multiple Planning Center organizations in one call.
- Writes plans, plan items, and plan times.
- Writes are preview-first: show the exact change, send nothing until you confirm, and never retry automatically. Deletes require an exact title/date/time guard.

## Safety

- Credentials stay in `~/.mpco_credentials.json`, never in the repo.
- Reads are GET-only.
- If a write times out, check Planning Center before repeating it.

## Docs and license

- [Tool reference](docs/REFERENCE.md)
- [Advanced setup and troubleshooting](docs/ADVANCED.md)
- [Manual installation](docs/ADVANCED.md#manual-installation-and-update)
- [MIT License](LICENSE)
