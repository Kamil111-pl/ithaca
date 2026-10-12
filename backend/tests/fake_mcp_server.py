"""Minimal stdio MCP server used by the tests."""
from mcp.server.fastmcp import FastMCP

app = FastMCP("fake")


@app.tool()
def shout(text: str) -> str:
    """Return the text in upper case."""
    return text.upper()


if __name__ == "__main__":
    app.run()
