import sys
from pathlib import Path

import pytest

from ithaca import db
from ithaca.mcp_client import manager
from ithaca.tools.registry import ToolContext, registry


@pytest.mark.asyncio
async def test_stdio_server_tools_register_and_run():
    sid = db.new_id("s_")
    db.execute(
        "INSERT INTO mcp_servers (id, name, command, args, env, enabled, trusted, created_at) VALUES (?, 'fake', ?, ?, '{}', 1, 0, 0)",
        (sid, sys.executable, db.dumps([str(Path(__file__).with_name("fake_mcp_server.py"))])),
    )
    status = await manager.connect(sid)
    try:
        assert status["connected"] and status["tools"] == ["mcp__fake__shout"]
        tool = registry.get("mcp__fake__shout")
        assert tool.effects == frozenset({"external"}) and tool.untrusted_output
        result = await tool.handler({"text": "ithaca"}, ToolContext(run_id="r", conversation_id="c"))
        assert result.content == "ITHACA" and result.untrusted_source == "MCP server fake"
    finally:
        await manager.disconnect(sid)
    assert registry.get("mcp__fake__shout") is None
