"""agent-host: the web page and HTTP API in front of the host.

    uv run python server.py

Then open http://127.0.0.1:8000
"""

from __future__ import annotations

import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse

HERE = Path(__file__).parent
HOST, PORT = "127.0.0.1", 8000
DESK_LIMIT = 2

app = FastAPI(title="agent-host")


@app.get("/")
def page():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/api/state")
def state():
    """Everything the page shows, in one snapshot."""
    return {"now": time.time(), "desk_limit": DESK_LIMIT, "servers": [], "runs": []}


if __name__ == "__main__":
    print(f"agent-host on http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
