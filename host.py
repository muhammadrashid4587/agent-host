"""The host: keeps MCP servers connected, runs runs, and checks, forwards and logs every
tool call a run makes."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from mcp_client import Disconnected, MCPError, StdioClient
from store import Store

HERE = Path(__file__).parent
NOTES_COMMAND = "python notes_server.py"   # the sample server, connected on startup
DB_PATH = Path(os.environ.get("AGENT_HOST_DB", HERE / "agent_host.sqlite"))
DESK_LIMIT = 2         # runs that may be Running at once; the rest wait their turn
RUN_TIMEOUT = 60.0     # a run still running after this many seconds is marked failed


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
    status: str                 # pending | ok | error | rejected | cancelled
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
    state: str = "waiting"      # waiting | running | done
    outcome: str = ""           # once done: completed | cancelled | failed
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    calls: list[Call] = field(default_factory=list)
    stopped: asyncio.Event = field(default_factory=asyncio.Event, repr=False)  # set when done

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "tools": self.tools, "state": self.state,
                "outcome": self.outcome, "reason": self.reason, "created_at": self.created_at,
                "started_at": self.started_at, "ended_at": self.ended_at,
                "deadline": self.started_at + RUN_TIMEOUT if self.state == "running" else None,
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
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.store: Store | None = None
        self.servers: dict[int, Server] = {}
        self.runs: dict[int, Run] = {}
        self._server_ids = itertools.count(1)
        self._run_ids = itertools.count(1)
        self._call_ids = itertools.count(1)
        self._watchdog: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.store = Store(self.db_path)
        self._load()
        self._watchdog = asyncio.create_task(self._watch_deadlines())
        notes = next((s for s in self.servers.values() if s.builtin), None)
        if notes is None:
            notes = Server(next(self._server_ids), "notes", NOTES_COMMAND, builtin=True)
            self.servers[notes.id] = notes
        try:
            await self._open(notes)
        except HostError:
            pass  # stays on the page as disconnected, with the error
        # Reconnect the servers a person added before, without holding up the page.
        for server in self.servers.values():
            if not server.builtin:
                task = asyncio.create_task(self._reopen_quietly(server))
                self._background.add(task)
                task.add_done_callback(self._background.discard)

    async def stop(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
        for task in list(self._background):
            task.cancel()
        for server in self.servers.values():
            if server.client is not None:
                await server.client.close()
        if self.store is not None:
            self.store.close()

    def _load(self) -> None:
        """Put the board back the way it was. Runs that were running when the host stopped
        are marked failed, since their guests and in-flight calls are gone."""
        for row in self.store.servers():
            server = Server(row["id"], row["name"], row["command"], row["builtin"], "disconnected",
                            row["error"], row["tools"], row["info"])
            self.servers[server.id] = server
        for row in self.store.runs():
            run = Run(row["id"], row["name"], row["tools"], row["state"], row["outcome"], row["reason"],
                      row["created_at"], row["started_at"], row["ended_at"])
            self.runs[run.id] = run
        for row in self.store.calls():
            run = self.runs.get(row["run_id"])
            if run is not None:
                run.calls.append(Call(**row))
        restarted = time.time()
        for run in self.runs.values():
            if run.state == "running":
                run.state, run.outcome, run.ended_at = "done", "failed", restarted
                run.reason = "the host restarted while it was running"
                run.stopped.set()
                self.store.save_run(run)
            for call in run.calls:
                if call.status == "pending":
                    call.status, call.error = "error", "the host restarted before the server answered"
                    self.store.save_call(call)
        self._server_ids = itertools.count(max(self.servers, default=0) + 1)
        self._run_ids = itertools.count(max(self.runs, default=0) + 1)
        self._call_ids = itertools.count(max((c.id for r in self.runs.values() for c in r.calls), default=0) + 1)
        self._fill_desks()

    async def _reopen_quietly(self, server: Server) -> None:
        try:
            await self._open(server)
        except HostError:
            pass  # shown on the page with its error and a Reconnect button

    # ------------------------------------------------------------------ servers

    async def add_server(self, command: str, name: str = "") -> Server:
        """Connect a server from a typed command. Only kept if the handshake works."""
        argv_for(command)  # fail early on an empty or unparsable command
        wanted = clean_name(name)
        if wanted and self._name_taken(wanted):
            raise HostError(f"there is already a server called {wanted!r}")
        server = Server(next(self._server_ids), wanted or "server", command.strip())
        await self._open(server, save=False)
        if not wanted:
            server.name = self._unique_name(clean_name(server.info.get("name", "")) or
                                            clean_name(Path(argv_for(command)[-1]).stem) or "server")
        self.servers[server.id] = server
        self.store.save_server(server)
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
        self.store.delete_server(server_id)

    async def _open(self, server: Server, save: bool = True) -> None:
        """Start the server process and do the MCP handshake. save=False for a server that
        is not on the board yet (it is only kept if the handshake works)."""
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
            if save:
                self.store.save_server(server)
            raise HostError(f"{server.command}: {e}") from None
        server.client = client
        server.tools = client.tools
        server.info = {**client.server_info, "protocol": client.protocol_version}
        server.status = "connected"
        if save:
            self.store.save_server(server)

    def _lost(self, server: Server, client: StdioClient, reason: str) -> None:
        if server.client is client:
            server.client = None
            server.status, server.error = "disconnected", reason
            if server.id in self.servers:
                self.store.save_server(server)

    def _tools_changed(self, server: Server, tools: list[dict]) -> None:
        server.tools = tools
        if server.id in self.servers:
            self.store.save_server(server)

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
        self.runs[rid] = run
        self.store.save_run(run)
        self._fill_desks()
        return run

    def finish(self, run_id: int) -> Run:
        run = self.get_run(run_id)
        if run.state != "running":
            raise HostError(f"run {run_id} is {run.state}, so it cannot finish", 409)
        self._end(run, "completed", "finished by the guest")
        return run

    def cancel(self, run_id: int) -> Run:
        run = self.get_run(run_id)
        if run.state == "done":
            raise HostError(f"run {run_id} is already done", 409)
        self._end(run, "cancelled", f"cancelled while {run.state}")
        return run

    def _end(self, run: Run, outcome: str, reason: str) -> None:
        run.state, run.outcome, run.reason, run.ended_at = "done", outcome, reason, time.time()
        run.stopped.set()  # interrupts any call still waiting on a server
        self.store.save_run(run)
        self._fill_desks()

    def _fill_desks(self) -> None:
        """Move the oldest waiting runs to Running while a desk is free."""
        running = sum(r.state == "running" for r in self.runs.values())
        for run in sorted(self.runs.values(), key=lambda r: r.id):
            if running >= DESK_LIMIT:
                break
            if run.state == "waiting":
                run.state, run.started_at = "running", time.time()
                self.store.save_run(run)
                running += 1

    async def _watch_deadlines(self) -> None:
        while True:
            now = time.time()
            for run in list(self.runs.values()):
                if run.state == "running" and now - run.started_at >= RUN_TIMEOUT:
                    self._end(run, "failed", f"timed out: still running after {RUN_TIMEOUT:g} s")
            await asyncio.sleep(0.25)

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
        try:
            return call, await self._check_and_forward(run, call)
        finally:
            self.store.save_call(call)  # the call's final state, whatever happened

    async def _check_and_forward(self, run: Run, call: Call) -> int:
        """Fill in the call's outcome; returns the HTTP status for it."""
        qualified, arguments = call.tool, call.arguments
        # The checks. A rejected call is logged and never reaches an MCP server.
        if run.state != "running":
            call.error = f"rejected: run is {run.state}" + (" for a desk" if run.state == "waiting" else "")
            return 409
        if "/" not in qualified:
            call.error = "rejected: unknown or ambiguous tool name, give it as server/tool"
            return 403
        if qualified not in run.tools:
            call.error = "rejected: this run was not allowed to use this tool"
            return 403
        if not isinstance(arguments, dict):
            call.error = "rejected: arguments must be a JSON object"
            return 400

        call.allowed, call.status = True, "pending"
        server_name, tool_name = qualified.split("/", 1)
        server = next((s for s in self.servers.values() if s.name == server_name), None)
        if server is None or server.status != "connected" or server.client is None:
            call.status, call.error = "error", f"server {server_name} is not connected"
            return 502

        self.store.save_call(call)  # logged as pending while the server works
        start = time.monotonic()
        request = asyncio.ensure_future(server.client.call_tool(tool_name, arguments))
        stopped = asyncio.ensure_future(run.stopped.wait())
        try:
            # Wait for the server, but no longer than the run lives (cancel or timeout).
            await asyncio.wait({request, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if not request.done():
                request.cancel()  # the client tells the server with notifications/cancelled
                await asyncio.gather(request, return_exceptions=True)
                call.status, call.error = "cancelled", f"cancelled: run {run.outcome} ({run.reason})"
                return 409
            result = request.result()
        except MCPError as e:
            call.status, call.error = "error", f"server error: {e}"
            return 502
        except (Disconnected, TimeoutError) as e:
            call.status, call.error = "error", str(e)
            return 502
        except asyncio.CancelledError:  # the host is shutting down
            request.cancel()
            call.status, call.error = "cancelled", "the host shut down before the server answered"
            raise
        finally:
            stopped.cancel()
            call.ms = round((time.monotonic() - start) * 1000, 1)
        call.result = result
        if result.get("isError"):
            call.status, call.error = "error", result_text(result) or "the tool reported an error"
        else:
            call.status = "ok"
        return 200

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

    def snapshot(self, done_shown: int = 50) -> dict:
        """What the page shows: every server, every waiting or running run, and the most
        recent finished runs (older ones stay in the database and at /api/runs/{id})."""
        done = sorted((r for r in self.runs.values() if r.state == "done"), key=lambda r: r.ended_at or 0)
        hidden = {r.id for r in done[:-done_shown]} if len(done) > done_shown else set()
        return {"desk_limit": DESK_LIMIT, "run_timeout": RUN_TIMEOUT,
                "servers": [s.public() for s in self.servers.values()],
                "runs": [r.public() for r in self.runs.values() if r.id not in hidden]}
