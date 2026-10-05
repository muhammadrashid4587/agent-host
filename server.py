"""agent-host: the web page and HTTP API in front of the host.

    uv run python server.py

Then open http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from host import Host, HostError

HERE = Path(__file__).parent
HOST, PORT = "127.0.0.1", 8000

host = Host()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await host.start()
    yield
    await host.stop()


app = FastAPI(title="agent-host", lifespan=lifespan)


@app.exception_handler(HostError)
async def host_error(request: Request, exc: HostError):
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


class NewServer(BaseModel):
    command: str
    name: str = ""


class NewRun(BaseModel):
    name: str = ""
    tools: list[str] = Field(description='the "server/tool" names this run may call')
    max_calls: int | None = Field(None, description="how many calls the host forwards for the run; empty = no limit")
    timeout: float | None = Field(None, description="seconds the run may stay running (default 60)")


class ToolCall(BaseModel):
    tool: str = Field(description='"server/tool", or a bare tool name if it is unambiguous')
    arguments: object = Field(default_factory=dict)


@app.get("/")
async def page():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/api/state")
async def state():
    """Everything the page shows, in one snapshot."""
    return {"now": time.time(), **host.snapshot()}


@app.get("/api/events")
async def events():
    """Server-sent events: the same snapshot as /api/state, sent again whenever it changes."""
    async def stream():
        seen = -1
        while not host.closing:
            if host.version == seen:
                yield ": still here\n\n"  # keeps proxies and the browser from giving up
            else:
                seen = host.version
                yield f"data: {json.dumps({'now': time.time(), **host.snapshot()})}\n\n"
            await host.wait_for_change(seen, timeout=15)
            await asyncio.sleep(0.05)  # gather a burst of changes into one message
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/servers")
async def servers():
    return host.snapshot()["servers"]


@app.post("/api/servers", status_code=201)
async def add_server(body: NewServer):
    return (await host.add_server(body.command, body.name)).public()


@app.post("/api/servers/{server_id}/reconnect")
async def reconnect(server_id: int):
    return (await host.reconnect(server_id)).public()


@app.delete("/api/servers/{server_id}", status_code=204)
async def remove_server(server_id: int):
    await host.remove(server_id)


@app.get("/api/tools")
async def tools():
    """Every "server/tool" a run could be allowed to use right now."""
    return host.offered_tools()


@app.get("/api/runs")
async def runs():
    return [r.public() for r in host.runs.values()]


@app.post("/api/runs", status_code=201)
async def create_run(body: NewRun):
    return host.create_run(body.name, body.tools, body.max_calls, body.timeout).public()


@app.get("/api/runs/{run_id}")
async def get_run(run_id: int):
    return host.get_run(run_id).public()


@app.post("/api/runs/{run_id}/calls")
async def call_tool(run_id: int, body: ToolCall):
    """Ask the host to make one tool call for this run. The answer is the call's log entry;
    the status is 200 if the tool ran, 403 if the run may not use it, 409 if the run is not
    running, 429 if it used up its calls, and 502 if the MCP server could not answer."""
    call, status = await host.call(run_id, body.tool, body.arguments)
    return JSONResponse(call.public(), status_code=status)


@app.post("/api/runs/{run_id}/cancel")
async def cancel(run_id: int):
    return host.cancel(run_id).public()


@app.post("/api/runs/{run_id}/finish")
async def finish(run_id: int):
    return host.finish(run_id).public()


if __name__ == "__main__":
    print(f"agent-host on http://{HOST}:{PORT}")
    # Don't let an open tool call hold up Ctrl+C: after 2 s, open requests are dropped.
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning", timeout_graceful_shutdown=2)
