"""MCP (Model Context Protocol) client: local stdio servers and remote HTTP servers.

Transports:
- stdio: a command Ithaca starts as a child process.
- http:  Streamable HTTP (the current MCP remote transport).
- sse:   the older HTTP+SSE transport.
- auto:  try Streamable HTTP, fall back to SSE.

Remote auth: none, static headers (e.g. an API key), or OAuth 2.1 with PKCE
and dynamic client registration. For OAuth, Ithaca hands the browser the
provider's sign-in page; the provider redirects back to
/api/mcp/oauth/callback, and tokens are stored (and refreshed) in the DB.

Each server lives in its own task, because MCP transports use structured
concurrency: a connection must be opened and closed by the same task.

Tools are registered as `mcp__<server>__<tool>` with the `external` effect,
so untrusted servers require approval and taint the run they report into.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
from contextlib import AsyncExitStack
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import config, db
from .tools.registry import Tool, ToolContext, ToolResult, registry

log = logging.getLogger("ithaca.mcp")

CONNECT_WAIT_S = 20
OAUTH_TIMEOUT_S = 600


def _safe(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)[:40]


def _qualified(server: str, tool: str) -> str:
    """mcp__<server>__<tool>, kept within the 64-char limit providers enforce."""
    prefix = f"mcp__{_safe(server)[:20]}__"
    return prefix + _safe(tool)[: 64 - len(prefix)]


def _prefix(server: str) -> str:
    return f"mcp__{_safe(server)[:20]}__"


def redirect_uri() -> str:
    return f"http://127.0.0.1:{config.PORT}/api/mcp/oauth/callback"


def validate_remote_url(url: str) -> str | None:
    """Remote servers must use https, except on this machine."""
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        return "Enter a full URL, e.g. https://example.com/mcp"
    if p.scheme == "http":
        host = p.hostname
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == "localhost"
        if not local:
            return "Remote MCP servers must use https:// (plain http is only allowed on this machine)."
    return None


class DBTokenStorage:
    """TokenStorage for the MCP SDK's OAuth client, persisted in mcp_servers.oauth."""

    def __init__(self, server_id: str):
        self.server_id = server_id

    def _load(self) -> dict[str, Any]:
        row = db.query_one("SELECT oauth FROM mcp_servers WHERE id = ?", (self.server_id,))
        return db.loads(row["oauth"], {}) if row else {}

    def _save(self, key: str, value: Any) -> None:
        data = self._load()
        data[key] = value
        db.execute("UPDATE mcp_servers SET oauth = ? WHERE id = ?", (db.dumps(data), self.server_id))

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = self._load().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        self._save("tokens", tokens.model_dump(mode="json", exclude_none=True))

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        self._save("client_info", client_info.model_dump(mode="json", exclude_none=True))


class _Server:
    def __init__(self, row: dict[str, Any], manager: "MCPManager"):
        self.row = row
        self.manager = manager
        self.session = None
        self.tools: list[str] = []
        self.error: str | None = None
        self.state = "connecting"         # connecting | auth_required | connected | error | stopped
        self.auth_url: str | None = None
        self.transport_used: str | None = None
        self.ready = asyncio.Event()      # set once connected, failed, or waiting for sign-in
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name=f"mcp-{self.row['name']}")

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task and not self.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=8)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                self.task.cancel()
        self._unregister()
        self.state = "stopped"

    def _unregister(self) -> None:
        registry.unregister_prefix(_prefix(self.row["name"]))
        self.tools = []
        self.session = None

    async def _run(self) -> None:
        transport = self.row.get("transport") or "stdio"
        candidates = ["http", "sse"] if transport == "auto" else [transport]
        last_exc: Exception | None = None
        for t in candidates:
            try:
                async with AsyncExitStack() as stack:
                    read, write = await self._open(stack, t)
                    from mcp import ClientSession

                    session = await stack.enter_async_context(ClientSession(read, write))
                    await asyncio.wait_for(session.initialize(), timeout=OAUTH_TIMEOUT_S if self.row.get("auth") == "oauth" else 30)
                    listed = await session.list_tools()
                    self.session, self.transport_used = session, t
                    self._register(listed.tools)
                    self.state, self.error, self.auth_url = "connected", None, None
                    self.ready.set()
                    await self.stop_event.wait()
                    return
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - anyio wraps errors in ExceptionGroups
                last_exc = exc
                if self.stop_event.is_set():
                    return
                log.info("MCP %s via %s failed: %s", self.row["name"], t, _describe(exc))
            finally:
                self._unregister()
        self.state = "error"
        self.error = _describe(last_exc) if last_exc else "Could not connect."
        self.ready.set()

    async def _open(self, stack: AsyncExitStack, transport: str):
        if transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=self.row["command"],
                args=db.loads(self.row["args"], []),
                env={**os.environ, **db.loads(self.row["env"], {})},
            )
            return await stack.enter_async_context(stdio_client(params))

        import httpx

        url = self.row["url"]
        headers = db.loads(self.row.get("headers"), {}) if self.row.get("auth") == "headers" else {}
        auth = self._oauth_provider() if self.row.get("auth") == "oauth" else None
        if transport == "sse":
            from mcp.client.sse import sse_client

            read, write = await stack.enter_async_context(sse_client(url, headers=headers, auth=auth, timeout=30))
            return read, write
        from mcp.client.streamable_http import streamable_http_client

        client = await stack.enter_async_context(httpx.AsyncClient(
            headers=headers, auth=auth, timeout=httpx.Timeout(60.0, read=300.0)))
        read, write, _ = await stack.enter_async_context(streamable_http_client(url, http_client=client))
        return read, write

    def _oauth_provider(self):
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata

        manager, server = self.manager, self

        async def redirect_handler(auth_url: str) -> None:
            state = parse_qs(urlparse(auth_url).query).get("state", [""])[0]
            server.auth_url, server.state = auth_url, "auth_required"
            manager.pending_oauth[state] = (server.row["id"], asyncio.get_running_loop().create_future())
            server.ready.set()     # tell the API caller to show the sign-in button

        async def callback_handler() -> tuple[str, str | None]:
            state = next((s for s, (sid, _) in manager.pending_oauth.items() if sid == server.row["id"]), None)
            if state is None:
                raise RuntimeError("No sign-in in progress.")
            _, fut = manager.pending_oauth[state]
            try:
                code, returned_state = await asyncio.wait_for(fut, timeout=OAUTH_TIMEOUT_S)
            finally:
                manager.pending_oauth.pop(state, None)
            server.state, server.auth_url = "connecting", None
            return code, returned_state

        metadata = OAuthClientMetadata(
            client_name="Ithaca",
            redirect_uris=[redirect_uri()],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
        return OAuthClientProvider(
            server_url=self.row["url"],
            client_metadata=metadata,
            storage=DBTokenStorage(self.row["id"]),
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            timeout=OAUTH_TIMEOUT_S,
        )

    # ------------------------------------------------------------ tools
    def _register(self, tools) -> None:
        registry.unregister_prefix(_prefix(self.row["name"]))
        trusted = bool(self.row["trusted"])
        for t in tools:
            qualified = _qualified(self.row["name"], t.name)
            registry.register(Tool(
                name=qualified,
                description=(t.description or t.name)[:1000] + f" (MCP server: {self.row['name']})",
                parameters=t.inputSchema or {"type": "object", "properties": {}},
                handler=self._make_handler(t.name),
                effects=frozenset({"external"}),
                group=f"mcp:{self.row['name']}",
                untrusted_output=not trusted,
                trusted_external=trusted,
                keywords=self.row["name"],
            ))
            self.tools.append(qualified)

    def _make_handler(self, tool_name: str):
        async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            if self.session is None:
                return ToolResult("MCP server is not connected.", is_error=True)
            result = await asyncio.wait_for(self.session.call_tool(tool_name, args), timeout=120)
            parts = []
            for block in result.content:
                if getattr(block, "type", "") == "text":
                    parts.append(block.text)
                else:
                    parts.append(f"[{getattr(block, 'type', 'content')} omitted]")
            text = "\n".join(parts) or "(no output)"
            source = None if self.row["trusted"] else f"MCP server {self.row['name']}"
            return ToolResult(text, is_error=bool(getattr(result, "isError", False)), untrusted_source=source)

        return handler


def _describe(exc: BaseException | None) -> str:
    """Flatten anyio ExceptionGroups into the first useful message."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if exc is None:
        return "Unknown error"
    msg = str(exc) or exc.__class__.__name__
    if "401" in msg or "Unauthorized" in msg:
        msg += " — the server needs authentication (try OAuth sign-in or an API key header)."
    return msg[:500]


class MCPManager:
    def __init__(self) -> None:
        self.servers: dict[str, _Server] = {}
        # OAuth state → (server id, future resolved by the callback route)
        self.pending_oauth: dict[str, tuple[str, asyncio.Future]] = {}

    async def start_all(self) -> None:
        for row in db.query("SELECT * FROM mcp_servers WHERE enabled = 1"):
            try:
                await self.connect(row["id"], wait=False)
            except Exception as exc:  # noqa: BLE001
                log.warning("MCP server %s failed to start: %s", row["name"], exc)

    async def connect(self, server_id: str, wait: bool = True) -> dict[str, Any]:
        row = db.query_one("SELECT * FROM mcp_servers WHERE id = ?", (server_id,))
        if not row:
            raise KeyError(server_id)
        await self.disconnect(server_id)
        srv = _Server(row, self)
        self.servers[server_id] = srv
        srv.start()
        if wait:
            try:
                await asyncio.wait_for(srv.ready.wait(), timeout=CONNECT_WAIT_S)
            except asyncio.TimeoutError:
                pass
        return self.status(server_id)

    async def disconnect(self, server_id: str) -> None:
        srv = self.servers.pop(server_id, None)
        if srv:
            for state, (sid, fut) in list(self.pending_oauth.items()):
                if sid == server_id:
                    fut.cancel()
                    self.pending_oauth.pop(state, None)
            await srv.stop()

    async def stop_all(self) -> None:
        for sid in list(self.servers):
            await self.disconnect(sid)

    def complete_oauth(self, state: str, code: str) -> bool:
        entry = self.pending_oauth.get(state)
        if not entry or entry[1].done():
            return False
        entry[1].set_result((code, state))
        return True

    def status(self, server_id: str) -> dict[str, Any]:
        srv = self.servers.get(server_id)
        if not srv:
            return {"connected": False, "state": "stopped", "tools": [], "error": None, "auth_url": None}
        return {"connected": srv.session is not None, "state": srv.state, "tools": srv.tools,
                "error": srv.error, "auth_url": srv.auth_url, "transport_used": srv.transport_used}


manager = MCPManager()
