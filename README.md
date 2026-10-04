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
