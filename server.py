"""agent-host: the web page and HTTP API in front of the host.

    uv run python server.py

Then open http://127.0.0.1:8000
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from host import Host, HostError

HERE = Path(__file__).parent
HOST, PORT = "127.0.0.1", 8000
DESK_LIMIT = 2

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


@app.get("/")
def page():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/api/state")
def state():
    """Everything the page shows, in one snapshot."""
    return {"now": time.time(), "desk_limit": DESK_LIMIT, "runs": [], **host.snapshot()}


@app.get("/api/servers")
def servers():
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


if __name__ == "__main__":
    print(f"agent-host on http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
