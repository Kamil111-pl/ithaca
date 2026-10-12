"""Remote MCP test server: python fake_remote_mcp.py <port> <streamable-http|sse> [required-api-key]."""
import sys

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

port, transport = int(sys.argv[1]), sys.argv[2]
key = sys.argv[3] if len(sys.argv) > 3 else None
app = FastMCP("remote-fake", host="127.0.0.1", port=port)


@app.tool()
def add(a: int, b: int) -> str:
    """Add two numbers."""
    return str(a + b)


asgi = app.streamable_http_app() if transport == "streamable-http" else app.sse_app()


async def guarded(scope, receive, send):
    if key and scope["type"] == "http":
        headers = dict(scope.get("headers") or [])
        if headers.get(b"x-api-key", b"").decode() != key:
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
    await asgi(scope, receive, send)


uvicorn.run(guarded, host="127.0.0.1", port=port, log_level="warning")
