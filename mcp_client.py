"""A minimal MCP client over stdio.

The host starts each MCP server as a child process and talks JSON-RPC 2.0 with it, one
message per line on stdin/stdout: initialize, notifications/initialized, tools/list and
tools/call. Whatever the server writes to stderr is kept to explain failures.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import signal
from collections import deque

PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
CLIENT_INFO = {"name": "agent-host", "version": "0.1.0"}
LINE_LIMIT = 16 * 1024 * 1024  # longest single message we accept from a server


class MCPError(Exception):
    """The server answered a request with a JSON-RPC error."""

    def __init__(self, code: int, message: str, data=None):
        super().__init__(f"{message} (code {code})")
        self.code, self.data = code, data


class Disconnected(Exception):
    """The server process is not running (it never started, exited, or was closed)."""


class StdioClient:
    def __init__(self, argv: list[str], cwd: str | None = None, on_exit=None, on_tools_changed=None):
        self.argv, self.cwd = argv, cwd
        self.on_exit = on_exit                  # called with a reason if the server dies by itself
        self.on_tools_changed = on_tools_changed
        self.proc: asyncio.subprocess.Process | None = None
        self.server_info: dict = {}
        self.protocol_version = ""
        self.tools: list[dict] = []
        self.ready = False    # handshake finished
        self.closed = False
        self.exit_reason = ""
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._stderr: deque[str] = deque(maxlen=20)
        self._noise: deque[str] = deque(maxlen=5)   # stdout lines that were not JSON-RPC
        self._closing = False
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------ lifecycle

    async def start(self, timeout: float = 10.0) -> None:
        """Spawn the server and do the MCP handshake. Raises Disconnected with a readable
        reason if anything goes wrong; the process is cleaned up in that case."""
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv, cwd=self.cwd, limit=LINE_LIMIT, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            self.closed = True
            raise Disconnected(f"could not start {self.argv[0]!r}: {e.strerror or e}") from None
        self._tasks = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr())]
        try:
            init = await self.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            }, timeout=timeout)
            version = init.get("protocolVersion")
            if version not in SUPPORTED_VERSIONS:
                raise Disconnected(f"server answered with MCP protocol version {version!r}, "
                                   f"which this host does not speak ({', '.join(SUPPORTED_VERSIONS)})")
            self.protocol_version = version
            self.server_info = init.get("serverInfo") or {}
            await self.notify("notifications/initialized")
            if "tools" in (init.get("capabilities") or {}):
                self.tools = await self.list_tools(timeout=timeout)
            self.ready = True
        except BaseException as e:
            reason = self._explain(e, "handshake")
            await self.close()
            if isinstance(e, asyncio.CancelledError):
                raise
            raise Disconnected(reason) from None

    async def close(self) -> None:
        """Stop the server: close its stdin, then SIGTERM, then SIGKILL its process group."""
        self._closing = True
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            for sig in (None, signal.SIGTERM, signal.SIGKILL):
                if sig is not None:
                    try:
                        os.killpg(proc.pid, sig)
                    except (ProcessLookupError, PermissionError):
                        pass
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                    break
                except TimeoutError:
                    continue
        for task in self._tasks:
            if task is asyncio.current_task():
                continue
            try:
                await asyncio.wait_for(task, 2)
            except (TimeoutError, asyncio.CancelledError, Exception):
                task.cancel()
        self._gone(self.exit_reason or "closed by the host")

    # ------------------------------------------------------------------ requests

    async def request(self, method: str, params: dict | None = None, timeout: float | None = None):
        if self.closed:
            raise Disconnected(self.exit_reason or "not connected")
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._write(msg)
            await self.proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            self._cancel_remote(rid, "timed out")
            raise TimeoutError(f"{method} got no answer within {timeout:g} s") from None
        except asyncio.CancelledError:
            self._cancel_remote(rid, "cancelled by the host")
            raise
        except (BrokenPipeError, ConnectionResetError):
            raise Disconnected(self.exit_reason or "server closed its input") from None
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._write(msg)
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise Disconnected(self.exit_reason or "server closed its input") from None

    async def list_tools(self, timeout: float | None = None) -> list[dict]:
        tools, cursor = [], None
        while True:
            page = await self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=timeout)
            tools.extend(page.get("tools") or [])
            cursor = page.get("nextCursor")
            if not cursor:
                return tools

    async def call_tool(self, name: str, arguments: dict, timeout: float | None = None) -> dict:
        return await self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)

    # ------------------------------------------------------------------ internals

    def _write(self, msg: dict) -> None:
        if self.closed or self.proc is None or self.proc.stdin.is_closing():
            raise Disconnected(self.exit_reason or "not connected")
        self.proc.stdin.write(json.dumps(msg, separators=(",", ":")).encode() + b"\n")

    def _cancel_remote(self, rid: int, reason: str) -> None:
        """Tell the server we no longer want the answer to request rid."""
        try:
            self._write({"jsonrpc": "2.0", "method": "notifications/cancelled",
                         "params": {"requestId": rid, "reason": reason}})
        except Exception:
            pass

    async def _read_stdout(self) -> None:
        reason = ""
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    self._noise.append(line[:200].decode(errors="replace"))
                    continue
                for m in msg if isinstance(msg, list) else [msg]:
                    if isinstance(m, dict):
                        self._handle(m)
        except (ValueError, asyncio.LimitOverrunError):
            reason = "server sent a message longer than 16 MB"
        code = await self.proc.wait() if not reason else None
        try:  # let the stderr reader catch the server's last words
            await asyncio.wait_for(asyncio.shield(self._tasks[1]), 1)
        except (TimeoutError, Exception):
            pass
        if not self._closing:
            if code is None:
                await self.close()
            self._gone(reason or f"server exited with code {code}", by_itself=True)

    async def _read_stderr(self) -> None:
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            if text:
                self._stderr.append(text[:300])

    def _handle(self, msg: dict) -> None:
        if "method" not in msg:  # a response to one of our requests
            fut = self._pending.get(msg.get("id"))
            if fut is None or fut.done():
                return
            if "error" in msg:
                err = msg["error"] or {}
                fut.set_exception(MCPError(err.get("code", 0), err.get("message", "error"), err.get("data")))
            else:
                fut.set_result(msg.get("result") or {})
            return
        if "id" in msg:  # a request from the server
            if msg["method"] == "ping":
                reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
            else:
                reply = {"jsonrpc": "2.0", "id": msg["id"],
                         "error": {"code": -32601, "message": f"method not supported by agent-host: {msg['method']}"}}
            try:
                self._write(reply)
            except Exception:
                pass
            return
        if msg["method"] == "notifications/tools/list_changed" and self.on_tools_changed:
            asyncio.get_running_loop().create_task(self._refresh_tools())

    async def _refresh_tools(self) -> None:
        try:
            self.tools = await self.list_tools(timeout=10)
        except Exception:
            return
        self.on_tools_changed(self.tools)

    def _gone(self, reason: str, by_itself: bool = False) -> None:
        if self.closed:
            return
        self.closed = True
        self.exit_reason = self._with_output(reason)
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(Disconnected(self.exit_reason))
        if by_itself and self.ready and self.on_exit:
            self.on_exit(self.exit_reason)

    def _with_output(self, reason: str) -> str:
        """Add the server's last words (stderr, stray stdout) to an error message."""
        parts = [reason]
        if self._stderr:
            parts.append("stderr: " + " | ".join(list(self._stderr)[-4:]))
        if self._noise:
            parts.append("stdout (not MCP): " + " | ".join(self._noise))
        return "; ".join(parts)

    def _explain(self, e: BaseException, stage: str) -> str:
        if isinstance(e, Disconnected):
            return str(e)
        if isinstance(e, MCPError):
            return self._with_output(f"server refused the {stage}: {e}")
        if isinstance(e, TimeoutError):
            return self._with_output(f"{e} (is this an MCP server speaking stdio?)")
        if isinstance(e, asyncio.CancelledError):
            return "cancelled"
        return self._with_output(f"{stage} failed: {e!r}")
