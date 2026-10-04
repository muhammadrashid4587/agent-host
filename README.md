# agent-host

A small host for agents and MCP servers. Plug in MCP servers, see the tools they offer, start
runs, and let each run call only the tools it was allowed. The host checks every call,
forwards it to the right MCP server and writes it to a live log.

## Run

```sh
uv run python server.py
```

Then open http://127.0.0.1:8000
