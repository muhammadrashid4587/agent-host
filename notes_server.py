"""Sample MCP server: a tiny notes store, spoken over stdio.

The host starts this on its own. To run it by hand (it then waits for MCP messages on stdin):

    uv run python notes_server.py

Notes live in memory, so they reset when the server restarts.
"""

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

mcp = MCPServer("notes", instructions="Keep short notes: add one, list them, read one back by id.")
NOTES: dict[int, dict] = {}


@mcp.tool()
def add_note(title: str, body: str) -> dict:
    """Add a note and return it with its new id."""
    note = {"id": len(NOTES) + 1, "title": title, "body": body}
    NOTES[note["id"]] = note
    return note


@mcp.tool()
def list_notes() -> list[dict]:
    """List all notes (id and title)."""
    return [{"id": n["id"], "title": n["title"]} for n in NOTES.values()]


@mcp.tool()
def read_note(id: int) -> dict:
    """Read one note by its id."""
    if id not in NOTES:
        raise ToolError(f"no note with id {id}")
    return NOTES[id]


if __name__ == "__main__":
    mcp.run()
