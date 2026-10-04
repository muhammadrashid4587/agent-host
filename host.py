"""The host: keeps MCP servers connected and knows which tools they offer."""

from __future__ import annotations

import itertools
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from mcp_client import Disconnected, StdioClient

HERE = Path(__file__).parent
NOTES_COMMAND = "python notes_server.py"   # the sample server, connected on startup


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
        self._server_ids = itertools.count(1)

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

    # ------------------------------------------------------------------ view

    def snapshot(self) -> dict:
        return {"servers": [s.public() for s in self.servers.values()]}
