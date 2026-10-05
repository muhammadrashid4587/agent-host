# agent-host

A small host for agents and MCP servers. Plug in MCP servers, see the tools they offer, start
runs, and let each run call only the tools it was allowed. The host checks every call,
forwards it to the right MCP server and writes it to a live log.

## Run

```sh
uv run python server.py
```

Then open http://127.0.0.1:8000

The page updates live: the host pushes every change to it over server-sent events.

In a second terminal, run the demo guest. It starts a run allowed to use the notes tools, adds a
note, lists the notes, reads the note back and finishes, all through the host's HTTP API:

```sh
uv run python demo_client.py
```

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

A run can also get limits when it starts. **Call limit** is how many calls the host will forward
for it; past that, calls are rejected (rejected calls don't count toward it). **Timeout** is how many
seconds it may stay running, from 1 to 600, 60 by default. A finished run has **Run again**, which
starts a new run with the same tools and limits.

At most 2 runs are Running at once (the desk limit). New runs wait in Waiting and start in
order as desks free up. **Cancel** stops a run, waiting or running. A run still running after
its timeout (60 seconds unless it picked another) is marked failed. A call still waiting on a server when its run stops is cancelled,
and the server is told so with `notifications/cancelled`.

Guests (agents) use the same HTTP API as the page:

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/api/state` | servers, their tools, runs and call logs |
| GET | `/api/events` | the same snapshot as server-sent events, pushed whenever anything changes |
| GET | `/api/tools` | every `server/tool` a run can be allowed |
| POST | `/api/runs` | start a run: `{"name": "...", "tools": ["notes/add_note", ...]}`, optionally with `"max_calls"` and `"timeout"` |
| GET | `/api/runs/{id}` | one run with its call log |
| POST | `/api/runs/{id}/calls` | call a tool: `{"tool": "notes/add_note", "arguments": {...}}` |
| POST | `/api/runs/{id}/finish` | mark the run done |
| POST | `/api/runs/{id}/cancel` | cancel the run |
| POST | `/api/servers` | connect a server: `{"command": "...", "name": "..."}` |

A call answers with its log entry. The HTTP status is 200 if the tool ran (check `status` for
`ok` or `error`), 403 if the run may not use that tool, 409 if the run is not running (still waiting, or done), 429 if it used up its call limit, and 502 if
the MCP server could not answer.

## Storage

Servers, runs and call logs are saved in `agent_host.sqlite` next to `server.py`, so refreshing
the page or restarting the host keeps the board. On restart the host reconnects the saved servers.
Runs that were running are marked failed, since their guests and calls are gone, and waiting runs
keep their place in line. Delete the file to start fresh. Set `AGENT_HOST_DB` to use another path.

## Tests

```sh
uv run pytest
```

The tests start a real host on a temporary database with the notes server and a spy MCP server
(`tests/spy_server.py`) that writes down every call it receives. That is how they check that a
rejected call never reaches a server, along with the desk limit, cancel, the timeout and restarts.
