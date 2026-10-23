# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Runs that offer tools the function middleware never sees fail loudly before the model is called."""

from __future__ import annotations

import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from agent_framework import FunctionInvocationContext, FunctionMiddleware, FunctionTool, MCPStdioTool, Message

from agent_framework_acgs import ACGSConfigurationError, ACGSGuard, UngovernedToolsError
from tests.fakes import (
    BLOCKED,
    HELLO,
    LEAK,
    REFUND,
    NoChatMiddlewareClient,
    ScriptedChatClient,
    Tools,
    agent,
    call,
    results,
    review_rules,
    run,
)

WEB_SEARCH = {"type": "web_search"}


def _declaration_only() -> FunctionTool:
    return FunctionTool(name="client_side", description="Runs in the caller.", input_model={"type": "object"})


def test_ungoverned_tools_lists_what_acgs_cannot_check() -> None:
    tools = Tools()
    mcp = MCPStdioTool(name="mail", command=sys.executable, args=["-c", "pass"])
    offered = [
        tools.send_email(),
        mcp,
        WEB_SEARCH,
        {"name": "code_interpreter"},
        {},
        object(),
        _declaration_only(),
    ]
    assert ACGSGuard.ungoverned_tools(offered) == [
        "hosted tool 'web_search'",
        "hosted tool 'code_interpreter'",
        "hosted tool 'unknown'",
        "tool object of type object",
        "declaration-only function tool 'client_side'",
    ]
    assert ACGSGuard.ungoverned_tools(None) == []
    assert ACGSGuard.ungoverned_tools(tools.send_email()) == []


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("ungoverned", ["hosted", "declaration-only"])
async def test_ungoverned_tools_are_refused_before_the_model_is_called(stream: bool, ungoverned: str) -> None:
    tools = Tools()
    extra: Any = WEB_SEARCH if ungoverned == "hosted" else _declaration_only()
    client = ScriptedChatClient(script=["Done."])
    governed = agent(client, [tools.send_email(), extra], ACGSGuard().middleware)
    with pytest.raises(UngovernedToolsError, match=f"ACGS cannot check calls of: {ungoverned}"):
        await run(governed, "Hi", stream=stream)
    assert client.requests == []


async def test_tools_offered_for_one_run_are_checked() -> None:
    client = ScriptedChatClient(script=["Done."])
    governed = agent(client, [Tools().send_email()], ACGSGuard().middleware)
    with pytest.raises(UngovernedToolsError, match="web_search"):
        await governed.run("Hi", tools=[WEB_SEARCH])
    assert client.requests == []


async def test_allow_ungoverned_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    client = ScriptedChatClient(script=["Done."])
    governed = agent(client, [Tools().send_email(), WEB_SEARCH], ACGSGuard(allow_ungoverned=True).middleware)
    with caplog.at_level(logging.WARNING, logger="agent_framework_acgs"):
        response = await governed.run("Hi")
    assert response.text == "Done."
    assert "ACGS does not check calls of: hosted tool 'web_search' (allow_ungoverned is set)." in caplog.text


async def test_additional_tools_are_refused() -> None:
    tools = Tools()
    client = ScriptedChatClient(script=["Done."])
    client.function_invocation_configuration["additional_tools"] = [tools.list_files()]
    governed = agent(client, [tools.send_email()], ACGSGuard().middleware)
    with pytest.raises(UngovernedToolsError, match=r"additional tool 'list_files' \(the caller runs it\)"):
        await governed.run("Hi")
    assert client.requests == []


async def test_disabled_function_invocation_is_refused() -> None:
    client = ScriptedChatClient(script=["Done."])
    client.function_invocation_configuration["enabled"] = False
    governed = agent(client, [Tools().send_email()], ACGSGuard().middleware)
    with pytest.raises(UngovernedToolsError, match="function invocation is off or missing"):
        await governed.run("Hi")
    assert client.requests == []


async def test_disabled_function_invocation_without_tools_is_allowed() -> None:
    client = ScriptedChatClient(script=["Done."])
    client.function_invocation_configuration["enabled"] = False
    response = await agent(client, [], ACGSGuard().middleware).run("Hi")
    assert response.text == "Done."


async def test_a_client_without_function_invocation_is_refused() -> None:
    from agent_framework import ChatMiddlewareLayer

    from tests.fakes import _ScriptedBase

    class ChatOnly(ChatMiddlewareLayer[Any], _ScriptedBase):
        pass

    client = ChatOnly(script=["Done."], middleware=[ACGSGuard().tool_check])
    with pytest.raises(UngovernedToolsError, match="function invocation is off or missing"):
        await client.get_response([Message(role="user", contents=["Hi"])], options={"tools": [Tools().send_email()]})
    assert client.requests == []
    response = await client.get_response([Message(role="user", contents=["Hi"])])
    assert response.text == "Done."


async def test_without_chat_middleware_support_function_middleware_still_checks_calls() -> None:
    tools = Tools()
    client = NoChatMiddlewareClient(script=[[call("send_email", LEAK, "e1")], "Done."])
    response = await agent(client, [tools.send_email()], ACGSGuard().middleware).run("Email")
    assert results(response)["e1"].startswith(BLOCKED)


async def test_the_function_middleware_refuses_calls_when_ungoverned_tools_are_offered() -> None:
    # Backstop for clients without chat middleware support: the first function call ends the run.
    tools = Tools()
    client = NoChatMiddlewareClient(script=[[call("send_email", HELLO, "e1")], "Done."])
    governed = agent(client, [tools.send_email(), WEB_SEARCH], ACGSGuard().middleware)
    with pytest.raises(UngovernedToolsError, match="web_search"):
        await governed.run("Email")
    assert tools.ran == []


async def test_function_middleware_alone_blocks_calls() -> None:
    tools = Tools()
    guard = ACGSGuard()
    client = ScriptedChatClient(script=[[call("send_email", LEAK, "e1")], "Done."])
    response = await agent(client, [tools.send_email()], [guard.function_middleware]).run("Email")
    assert results(response)["e1"].startswith(BLOCKED)


async def test_unbound_approvals_are_refused_on_a_client_without_chat_middleware_too() -> None:
    from agent_framework import ChatMiddlewareLayer

    from tests.fakes import _ScriptedBase

    class ChatOnly(ChatMiddlewareLayer[Any], _ScriptedBase):
        function_invocation_configuration = {"enabled": True, "disable_approval_response_binding": True}  # noqa: RUF012

    client = ChatOnly(script=["Done."], middleware=[ACGSGuard().tool_check])
    with pytest.raises(ACGSConfigurationError):
        await client.get_response([Message(role="user", contents=["Hi"])])


async def test_an_inner_agent_review_fails_closed() -> None:
    tools = Tools()
    inner_client = ScriptedChatClient(script=[[call("issue_refund", REFUND, "r1")], "Done."])
    inner = agent(inner_client, [tools.issue_refund()], ACGSGuard(constitution=review_rules()).middleware)
    outer_client = ScriptedChatClient(script=[[call("refunds", {"task": "Refund A-17"}, "o1")], "Done."])
    outer = agent(outer_client, [inner.as_tool(name="refunds", arg_name="task")], ACGSGuard().middleware)
    await outer.run("Refund", session=outer.create_session())
    assert tools.ran == []


needs_add_tools = pytest.mark.skipif(
    not hasattr(FunctionInvocationContext, "add_tools"), reason="add_tools is not in this agent-framework-core version"
)
experimental = pytest.mark.filterwarnings("ignore:.*add_tools is experimental")


@needs_add_tools
@experimental
async def test_tools_added_during_a_run_are_checked() -> None:
    tools = Tools()
    send_email = tools.send_email()

    class AddEmail(FunctionMiddleware):
        async def process(self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
            context.add_tools([send_email])
            await call_next()

    client = ScriptedChatClient(
        script=[[call("list_files", {"folder": "docs"}, "l1")], [call("send_email", LEAK, "e1")], "Done."]
    )
    response = await agent(client, [tools.list_files()], [AddEmail(), *ACGSGuard().middleware]).run("Go")
    assert results(response)["e1"].startswith(BLOCKED)
    assert tools.ran == ["list_files"]
    assert tools.sent == []


@needs_add_tools
@experimental
async def test_hosted_tools_added_during_a_run_are_refused() -> None:
    tools = Tools()

    class AddHosted(FunctionMiddleware):
        async def process(self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
            context.add_tools([WEB_SEARCH])
            await call_next()

    client = ScriptedChatClient(script=[[call("list_files", {"folder": "docs"}, "l1")], "Done."])
    with pytest.raises(UngovernedToolsError, match="web_search"):
        await agent(client, [tools.list_files()], [AddHosted(), *ACGSGuard().middleware]).run("Go")
    assert len(client.requests) == 1
