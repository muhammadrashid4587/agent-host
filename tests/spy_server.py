"""An MCP server for the tests. Every call that reaches it is appended to the file named by
SPY_LOG, so a test can prove which calls the host forwarded and which it never sent."""

import asyncio
import json
import os

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("spy")


def record(tool: str, **arguments) -> None:
    with open(os.environ["SPY_LOG"], "a") as f:
        f.write(json.dumps({"tool": tool, "arguments": arguments}) + "\n")


@mcp.tool()
def echo(text: str) -> str:
    """Return the text."""
    record("echo", text=text)
    return text


@mcp.tool()
def secret(text: str) -> str:
    """A tool the tests never allow."""
    record("secret", text=text)
    return "leaked: " + text


@mcp.tool()
async def slow(seconds: float) -> str:
    """Wait, then answer."""
    record("slow", seconds=seconds)
    await asyncio.sleep(seconds)
    return "done"


if __name__ == "__main__":
    mcp.run()
