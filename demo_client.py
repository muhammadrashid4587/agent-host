"""A guest agent for agent-host. It only talks to the host's HTTP API:

1. starts a run allowed to use the notes tools,
2. adds a note, lists the notes, and reads the new note back,
3. finishes the run.

Start the host first (uv run python server.py), then:

    uv run python demo_client.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

NOTES_TOOLS = ["notes/add_note", "notes/list_notes", "notes/read_note"]


class Host:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as res:
                return res.status, json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
        except urllib.error.URLError as e:
            sys.exit(f"Cannot reach the host at {self.url} ({e.reason}). Start it with: uv run python server.py")

    def call(self, run_id: int, tool: str, arguments: dict):
        """Ask the host to call a tool for our run; return the tool's answer as data."""
        status, call = self.request("POST", f"/api/runs/{run_id}/calls", {"tool": tool, "arguments": arguments})
        if status != 200 or call.get("status") != "ok":
            sys.exit(f"  {tool} failed ({status}): {call.get('error') or call}")
        result = call["result"]
        structured = result.get("structuredContent")
        if structured is not None:
            return structured.get("result", structured) if isinstance(structured, dict) else structured
        return json.loads(call["text"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Guest agent that uses the notes tools through agent-host.")
    parser.add_argument("--host", default="http://127.0.0.1:8000", help="agent-host URL")
    args = parser.parse_args()
    host = Host(args.host)

    _, offered = host.request("GET", "/api/tools")
    missing = [t for t in NOTES_TOOLS if t not in offered]
    if missing:
        sys.exit(f"The host does not offer {', '.join(missing)} right now (is the notes server connected?)")

    status, run = host.request("POST", "/api/runs", {"name": "demo guest", "tools": NOTES_TOOLS})
    if status != 201:
        sys.exit(f"Could not start a run: {run.get('error') or run}")
    rid = run["id"]
    print(f"Started run #{rid} allowed to use {', '.join(NOTES_TOOLS)}")

    # The host runs at most 2 runs at once; wait for a desk if needed.
    deadline = time.time() + 120
    while run["state"] == "waiting":
        if time.time() > deadline:
            host.request("POST", f"/api/runs/{rid}/cancel")
            sys.exit("Gave up waiting for a desk after 2 minutes (cancelled the run).")
        print("  waiting for a desk...")
        time.sleep(2)
        _, run = host.request("GET", f"/api/runs/{rid}")
    if run["state"] != "running":
        sys.exit(f"Run #{rid} ended before it started: {run['outcome']} ({run['reason']})")

    title, body = "Demo note", f"Written by demo_client.py at {time.strftime('%H:%M:%S')}"
    note = host.call(rid, "notes/add_note", {"title": title, "body": body})
    print(f"add_note   -> note {note['id']}: {note['title']!r}")

    notes = host.call(rid, "notes/list_notes", {})
    print(f"list_notes -> {len(notes)} note(s): " + ", ".join(f"{n['id']}:{n['title']!r}" for n in notes))

    back = host.call(rid, "notes/read_note", {"id": note["id"]})
    print(f"read_note  -> {back['body']!r}")

    host.request("POST", f"/api/runs/{rid}/finish")
    if back != note or not any(n["id"] == note["id"] for n in notes):
        sys.exit(f"The note read back does not match what was added (run #{rid} finished).")
    print(f"The note read back matches. Finished run #{rid}; its call log is at {host.url}")


if __name__ == "__main__":
    main()
