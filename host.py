"""The host: keeps MCP servers connected, runs runs, and checks, forwards and logs every
tool call a run makes."""

from __future__ import annotations

import itertools
import json
import re
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from mcp_client import Disconnected, MCPError, StdioClient

HERE = Path(__file__).parent
NOTES_COMMAND = "python notes_server.py"   # the sample server, connected on startup
CALL_TIMEOUT = 60.0                         # seconds the host waits for a tool result


class HostError(Exception):
    """A request the host turns down; the message is shown to the person."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Server:
    id: int
    name: str
    command: str
    builtin: bool = False
    status: str = "connecting"      # connecting | connected | disconnected
    error: str = ""
    tools: list[dict] = field(default_factory=list)
    info: dict = field(default_factory=dict)
    client: StdioClient | None = None

    def public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "command": self.command, "builtin": self.builtin,
            "status": self.status, "error": self.error, "info": self.info,
            "tools": [{"name": t.get("name", ""), "description": t.get("description") or "",
                       "input_schema": t.get("inputSchema") or {}} for t in self.tools],
        }


@dataclass
class Call:
    """One tool call a run asked the host to make, and what became of it."""
    id: int
    run_id: int
    at: float
    tool: str                   # "server/tool"
    arguments: object
    allowed: bool
    status: str                 # pending | ok | error | rejected
    result: dict | None = None  # the MCP tools/call result, as the server sent it
    error: str = ""
    ms: float | None = None

    def public(self) -> dict:
        return {"id": self.id, "run_id": self.run_id, "at": self.at, "tool": self.tool,
                "arguments": self.arguments, "allowed": self.allowed, "status": self.status,
                "result": self.result, "text": result_text(self.result), "error": self.error, "ms": self.ms}


@dataclass
class Run:
    id: int
    name: str
    tools: list[str]            # the "server/tool" names this run may call
    state: str = "running"      # running | done
    outcome: str = ""           # once done: completed
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    calls: list[Call] = field(default_factory=list)

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "tools": self.tools, "state": self.state,
                "outcome": self.outcome, "reason": self.reason, "created_at": self.created_at,
                "started_at": self.started_at, "ended_at": self.ended_at,
                "calls": [c.public() for c in self.calls]}


def result_text(result: dict | None) -> str:
    """The readable part of an MCP tool result."""
    if not result:
        return ""
    parts = []
    for item in result.get("content") or []:
        kind = item.get("type")
        if kind == "text":
            parts.append(item.get("text", ""))
        elif kind == "resource":
            res = item.get("resource") or {}
            parts.append(res.get("text") or f"[resource {res.get('uri', '')}]")
        else:
            parts.append(f"[{kind}]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"]))
    return "\n".join(parts)


def argv_for(command: str) -> list[str]:
    """Split a typed command. A leading `python` means this project's Python, so servers
    written against the project's dependencies just work."""
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise HostError(f"could not parse the command: {e}") from None
    if not argv:
        raise HostError("type a command that starts an MCP server, e.g. python notes_server.py")
    if argv[0] in ("python", "python3"):
        argv[0] = sys.executable
    return argv


def clean_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("-")[:40]


class Host:
    def __init__(self):
        self.servers: dict[int, Server] = {}
        self.runs: dict[int, Run] = {}
        self._server_ids = itertools.count(1)
        self._run_ids = itertools.count(1)
        self._call_ids = itertools.count(1)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        notes = Server(next(self._server_ids), "notes", NOTES_COMMAND, builtin=True)
        self.servers[notes.id] = notes
        try:
            await self._open(notes)
        except HostError:
            pass  # stays on the page as disconnected, with the error

    async def stop(self) -> None:
        for server in self.servers.values():
            if server.client is not None:
                await server.client.close()

    # ------------------------------------------------------------------ servers

    async def add_server(self, command: str, name: str = "") -> Server:
        """Connect a server from a typed command. Only kept if the handshake works."""
        argv_for(command)  # fail early on an empty or unparsable command
        wanted = clean_name(name)
        if wanted and self._name_taken(wanted):
            raise HostError(f"there is already a server called {wanted!r}")
        server = Server(next(self._server_ids), wanted or "server", command.strip())
        await self._open(server)
        if not wanted:
            server.name = self._unique_name(clean_name(server.info.get("name", "")) or
                                            clean_name(Path(argv_for(command)[-1]).stem) or "server")
        self.servers[server.id] = server
        return server

    async def reconnect(self, server_id: int) -> Server:
        server = self._server(server_id)
        if server.status == "connected":
            return server
        await self._open(server)
        return server

    async def remove(self, server_id: int) -> None:
        server = self._server(server_id)
        if server.builtin:
            raise HostError("the sample notes server stays connected")
        if server.client is not None:
            await server.client.close()
        del self.servers[server_id]

    async def _open(self, server: Server) -> None:
        """Start the server process and do the MCP handshake."""
        argv = argv_for(server.command)
        if server.builtin:
            argv = [sys.executable, str(HERE / "notes_server.py")]
        server.status, server.error = "connecting", ""
        client = StdioClient(argv, cwd=str(HERE),
                             on_exit=lambda reason: self._lost(server, client, reason),
                             on_tools_changed=lambda tools: self._tools_changed(server, tools))
        try:
            await client.start()
        except Disconnected as e:
            server.status, server.error = "disconnected", str(e)
            raise HostError(f"{server.command}: {e}") from None
        server.client = client
        server.tools = client.tools
        server.info = {**client.server_info, "protocol": client.protocol_version}
        server.status = "connected"

    def _lost(self, server: Server, client: StdioClient, reason: str) -> None:
        if server.client is client:
            server.client = None
            server.status, server.error = "disconnected", reason

    def _tools_changed(self, server: Server, tools: list[dict]) -> None:
        server.tools = tools

    def _server(self, server_id: int) -> Server:
        if server_id not in self.servers:
            raise HostError(f"no server with id {server_id}", 404)
        return self.servers[server_id]

    def _name_taken(self, name: str) -> bool:
        return any(s.name == name for s in self.servers.values())

    def _unique_name(self, base: str) -> str:
        name, n = base, 2
        while self._name_taken(name):
            name, n = f"{base}-{n}", n + 1
        return name

    def offered_tools(self) -> list[str]:
        """Every "server/tool" a connected server offers right now."""
        return [f"{s.name}/{t['name']}" for s in self.servers.values()
                if s.status == "connected" for t in s.tools]

    # ------------------------------------------------------------------ runs

    def create_run(self, name: str, tools: list[str]) -> Run:
        offered = self.offered_tools()
        tools = list(dict.fromkeys(t.strip() for t in tools if t.strip()))
        if not tools:
            raise HostError("pick at least one tool the run may use")
        unknown = [t for t in tools if t not in offered]
        if unknown:
            raise HostError(f"no connected server offers {', '.join(unknown)}")
        rid = next(self._run_ids)
        run = Run(rid, name.strip()[:60] or f"run {rid}", tools)
        run.started_at = time.time()
        self.runs[rid] = run
        return run

    def finish(self, run_id: int) -> Run:
        run = self.get_run(run_id)
        if run.state != "running":
            raise HostError(f"run {run_id} is {run.state}, so it cannot finish", 409)
        self._end(run, "completed", "finished by the guest")
        return run

    def _end(self, run: Run, outcome: str, reason: str) -> None:
        run.state, run.outcome, run.reason, run.ended_at = "done", outcome, reason, time.time()

    def get_run(self, run_id: int) -> Run:
        if run_id not in self.runs:
            raise HostError(f"no run with id {run_id}", 404)
        return self.runs[run_id]

    # ------------------------------------------------------------------ tool calls

    async def call(self, run_id: int, tool: str, arguments: object) -> tuple[Call, int]:
        """Check a run's tool call, forward it if allowed, and log both. Returns the log
        entry and the HTTP status that fits it."""
        run = self.get_run(run_id)
        qualified = self._qualify(run, str(tool).strip())
        call = Call(next(self._call_ids), run.id, time.time(), qualified, arguments, allowed=False, status="rejected")
        run.calls.append(call)

        # The checks. A rejected call is logged and never reaches an MCP server.
        if run.state != "running":
            call.error = f"rejected: run is {run.state}"
            return call, 409
        if "/" not in qualified:
            call.error = "rejected: unknown or ambiguous tool name, give it as server/tool"
            return call, 403
        if qualified not in run.tools:
            call.error = "rejected: this run was not allowed to use this tool"
            return call, 403
        if not isinstance(arguments, dict):
            call.error = "rejected: arguments must be a JSON object"
            return call, 400

        call.allowed, call.status = True, "pending"
        server_name, tool_name = qualified.split("/", 1)
        server = next((s for s in self.servers.values() if s.name == server_name), None)
        if server is None or server.status != "connected" or server.client is None:
            call.status, call.error = "error", f"server {server_name} is not connected"
            return call, 502

        start = time.monotonic()
        try:
            result = await server.client.call_tool(tool_name, arguments, timeout=CALL_TIMEOUT)
        except MCPError as e:
            call.status, call.error = "error", f"server error: {e}"
            return call, 502
        except (Disconnected, TimeoutError) as e:
            call.status, call.error = "error", str(e)
            return call, 502
        finally:
            call.ms = round((time.monotonic() - start) * 1000, 1)
        call.result = result
        if result.get("isError"):
            call.status, call.error = "error", result_text(result) or "the tool reported an error"
        else:
            call.status = "ok"
        return call, 200

    def _qualify(self, run: Run, tool: str) -> str:
        """Accept "server/tool", or a bare tool name when it is unambiguous."""
        if "/" in tool or not tool:
            return tool
        mine = [t for t in run.tools if t.split("/", 1)[1] == tool]
        if len(mine) == 1:
            return mine[0]
        anywhere = [t for t in self.offered_tools() if t.split("/", 1)[1] == tool]
        return anywhere[0] if len(anywhere) == 1 and not mine else tool

    # ------------------------------------------------------------------ view

    def snapshot(self) -> dict:
        return {"servers": [s.public() for s in self.servers.values()],
                "runs": [r.public() for r in self.runs.values()]}
