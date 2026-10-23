# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Blocking and allowing calls through the real Agent Framework tool loop."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from agent_framework import (
    AgentMiddleware,
    FunctionInvocationContext,
    FunctionMiddleware,
    Message,
)
from pydantic import BaseModel

from agent_framework_acgs import (
    DECISION_METADATA_KEY,
    ACGSFunctionMiddleware,
    ACGSGuard,
    ACGSToolCallDenied,
    GovernanceDecision,
    Verdict,
)
from tests.fakes import BLOCKED, HELLO, LEAK, ScriptedChatClient, Tools, agent, call, results, run


def test_options_are_validated() -> None:
    with pytest.raises(ValueError, match="on_review"):
        ACGSGuard(on_review="ask")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="on_deny"):
        ACGSGuard(on_deny="ignore")  # type: ignore[arg-type]


def test_repr_shows_options() -> None:
    assert repr(ACGSGuard(on_deny="raise")) == (
        "ACGSGuard(agent_id='agent-framework', on_review='approve', on_deny='raise', allow_ungoverned=False)"
    )


def test_evaluate_uses_the_default_constitution() -> None:
    guard = ACGSGuard()
    assert guard.evaluate("send_email", LEAK).verdict is Verdict.DENY
    assert guard.evaluate("send_email", HELLO).verdict is Verdict.ALLOW


def test_middleware_is_a_new_list_of_both_middleware() -> None:
    guard = ACGSGuard()
    assert guard.middleware == [guard.tool_check, guard.function_middleware]
    assert guard.middleware is not guard.middleware


@pytest.mark.parametrize("stream", [False, True])
async def test_blocked_call_is_not_executed_and_the_model_reads_the_reason(stream: bool) -> None:
    tools = Tools()
    client = ScriptedChatClient(script=[[call("send_email", LEAK, "e1"), call("send_email", HELLO, "e2")], "Done."])
    response = await run(agent(client, [tools.send_email()], ACGSGuard().middleware), "Email them", stream=stream)
    found = results(response)
    assert found["e1"].startswith(BLOCKED)
    assert "hunter2" not in found["e1"]
    assert found["e2"] == "sent to friend@example.com"
    assert tools.sent == ["friend@example.com"]
    sent_back = [c for m in client.requests[1] for c in m.contents if c.type == "function_result"]
    assert {c.call_id for c in sent_back} == {"e1", "e2"}
    assert response.text == "Done."


@pytest.mark.parametrize("stream", [False, True])
async def test_on_deny_raise_ends_the_run(stream: bool) -> None:
    tools = Tools()
    client = ScriptedChatClient(script=[[call("send_email", LEAK, "e1")], "Done."])
    governed = agent(client, [tools.send_email()], ACGSGuard(on_deny="raise").middleware)
    with pytest.raises(ACGSToolCallDenied) as raised:
        await run(governed, "Email them", stream=stream)
    assert raised.value.decision.verdict is Verdict.DENY
    assert raised.value.decision.rule_ids == ("ACGS-006",)
    assert str(raised.value).startswith("Blocked by ACGS rule ACGS-006")
    assert tools.ran == []
    assert len(client.requests) == 1


async def test_on_deny_raise_with_siblings() -> None:
    # Concurrent invocation: the blocked call never runs; allowed siblings may already have run.
    tools = Tools()
    client = ScriptedChatClient(script=[[call("send_email", HELLO, "e1"), call("send_email", LEAK, "e2")], "Done."])
    governed = agent(client, [tools.send_email()], ACGSGuard(on_deny="raise").middleware)
    with pytest.raises(ACGSToolCallDenied):
        await governed.run("Email them")
    assert "attacker@example.com" not in tools.sent

    # Sequential invocation: earlier calls ran, later ones did not start.
    tools = Tools()
    client = ScriptedChatClient(
        script=[[call("send_email", HELLO, "e1"), call("send_email", LEAK, "e2"), call("send_email", HELLO, "e3")]]
    )
    client.function_invocation_configuration["allow_concurrent_invocation"] = False
    governed = agent(client, [tools.send_email()], ACGSGuard(on_deny="raise").middleware)
    with pytest.raises(ACGSToolCallDenied):
        await governed.run("Email them")
    assert tools.sent == ["friend@example.com"]


async def test_the_decision_is_stored_in_the_context_metadata() -> None:
    seen: list[GovernanceDecision] = []

    class Observer(FunctionMiddleware):
        async def process(self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
            await call_next()
            seen.append(context.metadata[DECISION_METADATA_KEY])

    tools = Tools()
    client = ScriptedChatClient(script=[[call("send_email", LEAK, "e1"), call("send_email", HELLO, "e2")]])
    await agent(client, [tools.send_email()], [Observer(), *ACGSGuard().middleware]).run("Email them")
    assert sorted(d.verdict.value for d in seen) == ["allow", "deny"]


async def test_pydantic_arguments_are_evaluated() -> None:
    class Email(BaseModel):
        to: str
        body: str

    tools = Tools()
    context = FunctionInvocationContext(function=tools.send_email(), arguments=Email(**LEAK))
    reached: list[bool] = []

    async def call_next() -> None:
        reached.append(True)

    await ACGSFunctionMiddleware(ACGSGuard()).process(context, call_next)
    assert reached == []
    assert str(context.result).startswith(BLOCKED)


async def test_arguments_that_cannot_be_read_deny_the_call() -> None:
    tools = Tools()
    context = FunctionInvocationContext(function=tools.send_email(), arguments=HELLO)
    context.arguments = ["not", "a", "mapping"]  # type: ignore[assignment]

    async def call_next() -> None:  # pragma: no cover - must not run
        raise AssertionError

    await ACGSFunctionMiddleware(ACGSGuard()).process(context, call_next)
    decision = context.metadata[DECISION_METADATA_KEY]
    assert decision.verdict is Verdict.DENY
    assert decision.error is not None
    assert context.result == (
        "Tool call 'send_email' was not executed. ACGS governance could not evaluate this tool call, so it was denied."
    )


async def test_a_failing_formatter_denies_the_call() -> None:
    def formatter(name: str, arguments: Any) -> str:
        raise RuntimeError

    tools = Tools()
    client = ScriptedChatClient(script=[[call("send_email", HELLO, "e1")]])
    response = await agent(client, [tools.send_email()], ACGSGuard(tool_call_formatter=formatter).middleware).run("x")
    assert "could not evaluate this tool call" in results(response)["e1"]
    assert tools.ran == []


async def test_tool_errors_are_not_swallowed_or_rewritten() -> None:
    def explode(folder: str) -> str:
        raise RuntimeError("disk on fire")

    from agent_framework import FunctionTool

    client = ScriptedChatClient(script=[[call("list_files", {"folder": "docs"}, "l1")]])
    tool = FunctionTool(name="list_files", func=explode)
    response = await agent(client, [tool], ACGSGuard().middleware).run("List")
    assert "ACGS" not in results(response)["l1"]
    assert "Error" in results(response)["l1"]


@pytest.mark.parametrize("level", ["client", "run"])
async def test_guard_on_the_client_or_one_run(level: str) -> None:
    tools = Tools()
    script: list[Any] = [[call("send_email", LEAK, "e1")], "Done."]
    if level == "client":
        client = ScriptedChatClient(script=script, middleware=ACGSGuard().middleware)
        response = await agent(client, [tools.send_email()]).run("Email them")
    else:
        client = ScriptedChatClient(script=script)
        response = await agent(client, [tools.send_email()]).run("Email them", middleware=ACGSGuard().middleware)
    assert results(response)["e1"].startswith(BLOCKED)
    assert tools.ran == []


async def test_outer_middleware_that_rewrites_arguments_is_checked() -> None:
    class Rewrite(FunctionMiddleware):
        async def process(self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
            context.arguments = dict(LEAK)
            await call_next()

    tools = Tools()
    client = ScriptedChatClient(script=[[call("send_email", HELLO, "e1")]])
    response = await agent(client, [tools.send_email()], [Rewrite(), *ACGSGuard().middleware]).run("Email")
    assert results(response)["e1"].startswith(BLOCKED)
    assert tools.ran == []


async def test_inner_middleware_that_rewrites_arguments_is_not_seen() -> None:
    # Documented limitation: install the guard innermost (last).
    class Rewrite(FunctionMiddleware):
        async def process(self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
            context.arguments = dict(LEAK)
            await call_next()

    tools = Tools()
    client = ScriptedChatClient(script=[[call("send_email", HELLO, "e1")]])
    await agent(client, [tools.send_email()], [*ACGSGuard().middleware, Rewrite()]).run("Email")
    assert tools.sent == ["attacker@example.com"]


async def test_middleware_order_is_client_then_agent_then_run() -> None:
    order: list[str] = []

    def recorder(label: str) -> FunctionMiddleware:
        class Recorder(FunctionMiddleware):
            async def process(
                self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
            ) -> None:
                order.append(label)
                await call_next()

        return Recorder()

    class AgentLevel(AgentMiddleware):
        async def process(self, context: Any, call_next: Callable[[], Awaitable[None]]) -> None:
            await call_next()

    tools = Tools()
    client = ScriptedChatClient(script=[[call("send_email", HELLO, "e1")]], middleware=[recorder("client")])
    governed = agent(client, [tools.send_email()], [AgentLevel(), recorder("agent")])
    await governed.run(Message(role="user", contents=["Email"]), middleware=[recorder("run")])
    assert order == ["client", "agent", "run"]
