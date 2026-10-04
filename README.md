# agent-host

A small host for agents and MCP servers. Plug in MCP servers, see the tools they offer, start
runs, and let each run call only the tools it was allowed. The host checks every call,
forwards it to the right MCP server and writes it to a live log.

## Run

```sh
uv run python server.py
```

Then open http://127.0.0.1:8000

## MCP servers

The host speaks MCP over stdio (`initialize`, `tools/list`, `tools/call`). On startup it connects
the sample notes server in this repo, `notes_server.py`, which has `add_note`, `list_notes` and
`read_note`. Notes are kept in memory.

To connect another server, type the command that starts it in **Connect an MCP server**, for
example `python notes_server.py` or `uvx mcp-server-time`. Commands run in the project folder, and a
leading `python` means this project's Python. If the server fails the handshake, the error is
shown and it is not connected.

## Runs

Start a run on the page: give it a name and tick the tools it may use. While it is running, its
card has a small form to send one tool call through the host, and a live call log showing each
call's tool, arguments, result or error, and whether the host allowed it.

The host checks every call. A tool that was not ticked when the run started is rejected, logged,
and never sent to the MCP server. An allowed call is forwarded to the server that offers the
tool, and the result is logged.

Guests (agents) use the same HTTP API as the page:

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/api/state` | servers, their tools, runs and call logs |
| GET | `/api/tools` | every `server/tool` a run can be allowed |
| POST | `/api/runs` | start a run: `{"name": "...", "tools": ["notes/add_note", ...]}` |
| GET | `/api/runs/{id}` | one run with its call log |
| POST | `/api/runs/{id}/calls` | call a tool: `{"tool": "notes/add_note", "arguments": {...}}` |
| POST | `/api/runs/{id}/finish` | mark the run done |
| POST | `/api/servers` | connect a server: `{"command": "...", "name": "..."}` |

A call answers with its log entry. The HTTP status is 200 if the tool ran (check `status` for
`ok` or `error`), 403 if the run may not use that tool, 409 if the run is not running, and 502 if
the MCP server could not answer.
