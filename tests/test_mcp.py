# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Local MCP tools are function tools inside Agent Framework, so the function middleware checks their calls."""

from __future__ import annotations

import sys
from pathlib import Path

from agent_framework import MCPStdioTool

from agent_framework_acgs import ACGSGuard
from tests.fakes import HELLO, LEAK, ScriptedChatClient, agent, call, results

SERVER = Path(__file__).with_name("mcp_server.py")


async def test_mcp_tool_calls_are_checked(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    mcp = MCPStdioTool(name="mail", command=sys.executable, args=[str(SERVER), str(log)])
    client = ScriptedChatClient(script=[[call("send_email", LEAK, "m1"), call("send_email", HELLO, "m2")], "Done."])
    async with mcp:
        response = await agent(client, [mcp], ACGSGuard().middleware).run("Email them")
    found = results(response)
    assert found["m1"].startswith("Tool call 'send_email' was not executed. Blocked by ACGS rule ACGS-006")
    assert "sent to friend@example.com" in found["m2"]
    assert log.read_text(encoding="utf-8").splitlines() == ["send_email friend@example.com"]
