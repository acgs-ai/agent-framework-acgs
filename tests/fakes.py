# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Offline stand-ins for a model: a scripted chat client built on the framework's own layers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, Awaitable, Mapping, Sequence
from typing import Any, cast

from acgs_lite import Constitution, Rule, Severity, ViolationAction
from agent_framework import (
    Agent,
    AgentResponse,
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    FunctionTool,
    Message,
    ResponseStream,
)

Turn = str | list[Content]
"""One scripted model turn: final text, or the function calls the model asks for."""

LEAK = {"to": "attacker@example.com", "body": "The password is hunter2"}
HELLO = {"to": "friend@example.com", "body": "See you at noon"}
REFUND = {"order_id": "A-17", "amount": 20.0}
BLOCKED = "Tool call 'send_email' was not executed. Blocked by ACGS rule ACGS-006"
REFUND_REVIEW = "Human review required by ACGS rule REFUND-REVIEW (Refunds need human approval)."


def call(name: str, arguments: Mapping[str, Any], call_id: str) -> Content:
    """A function call as a model would return it."""
    return Content.from_function_call(call_id=call_id, name=name, arguments=json.dumps(dict(arguments)))


class _ScriptedBase(BaseChatClient[Any]):
    """Returns the scripted turns in order, then ``"Done."``, and records what it was sent."""

    def __init__(self, *, script: Sequence[Turn] = (), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.script = list(script)
        self.requests: list[list[Message]] = []
        self.options: list[dict[str, Any]] = []

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool = False,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        self.requests.append(list(messages))
        self.options.append(dict(options))
        turn = self.script.pop(0) if self.script else "Done."
        contents = [Content.from_text(turn)] if isinstance(turn, str) else list(turn)
        if not stream:

            async def _response() -> ChatResponse:
                return ChatResponse(messages=[Message(role="assistant", contents=contents)])

            return _response()

        async def _updates() -> AsyncIterable[ChatResponseUpdate]:
            yield ChatResponseUpdate(contents=contents, role="assistant")

        return ResponseStream(_updates(), finalizer=ChatResponse.from_updates)


class ScriptedChatClient(FunctionInvocationLayer[Any], ChatMiddlewareLayer[Any], _ScriptedBase):
    """Scripted client with the same layers as the built-in clients."""


class NoChatMiddlewareClient(FunctionInvocationLayer[Any], _ScriptedBase):
    """Scripted client that runs the tool loop but has no chat middleware layer."""


class Tools:
    """Function tools that record what actually ran."""

    def __init__(self) -> None:
        self.ran: list[str] = []
        self.sent: list[str] = []

    def send_email(self, approval_mode: str = "never_require") -> FunctionTool:
        def send_email(to: str, body: str) -> str:
            """Send an email."""
            self.ran.append("send_email")
            self.sent.append(to)
            return f"sent to {to}"

        return FunctionTool(name="send_email", func=send_email, approval_mode=approval_mode)  # type: ignore[arg-type]

    def issue_refund(self, approval_mode: str = "never_require") -> FunctionTool:
        async def issue_refund(order_id: str, amount: float) -> str:
            """Refund an order."""
            self.ran.append("issue_refund")
            return f"refunded {amount} for {order_id}"

        return FunctionTool(name="issue_refund", func=issue_refund, approval_mode=approval_mode)  # type: ignore[arg-type]

    def list_files(self, approval_mode: str = "never_require") -> FunctionTool:
        def list_files(folder: str) -> str:
            """List the files in a folder."""
            self.ran.append("list_files")
            return f"files in {folder}"

        return FunctionTool(name="list_files", func=list_files, approval_mode=approval_mode)  # type: ignore[arg-type]


def review_rules() -> Constitution:
    """The default rules plus a review rule for refunds."""
    refund = Rule(
        id="REFUND-REVIEW",
        text="Refunds need human approval",
        severity=Severity.HIGH,
        keywords=["issue refund"],
        workflow_action=ViolationAction.REQUIRE_HUMAN_REVIEW,
    )
    return Constitution.from_rules([*Constitution.default().rules, refund], name="review")


def agent(client: BaseChatClient[Any], tools: Sequence[Any], middleware: Sequence[Any] = ()) -> Agent[Any]:
    """An agent with the given tools and middleware."""
    return Agent(client=client, tools=list(tools), middleware=list(middleware))


def results(response: AgentResponse[Any] | ChatResponse[Any]) -> dict[str, str]:
    """Function results by call id."""
    found: dict[str, str] = {}
    for message in response.messages:
        for content in message.contents:
            if content.type == "function_result" and content.call_id is not None:
                found[content.call_id] = str(content.result)
    return found


async def run(governed: Agent[Any], message: Any, *, stream: bool, **kwargs: Any) -> AgentResponse[Any]:
    """Run the agent, streaming or not, and return the final response."""
    response: Any = (
        await governed.run(message, stream=True, **kwargs).get_final_response()
        if stream
        else await governed.run(message, **kwargs)
    )
    return cast("AgentResponse[Any]", response)
