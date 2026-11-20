"""Run allowed and denied Agent Framework tool calls offline, then print their decision trail."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import asdict
from typing import Any

# Disable telemetry before importing the framework; the scripted model needs no service or API key.
os.environ["OTEL_SDK_DISABLED"] = "true"

from agent_framework import (
    Agent,
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    Message,
    ResponseStream,
)

from agent_framework_acgs import ACGSGuard, GovernanceDecision


class AuditedGuard(ACGSGuard):
    """Collect public evaluation results without retaining argument values."""

    def __init__(self) -> None:
        super().__init__()
        self.decisions: list[GovernanceDecision] = []

    def evaluate(self, tool_name: str, arguments: Any) -> GovernanceDecision:
        decision = super().evaluate(tool_name, arguments)
        self.decisions.append(decision)
        return decision


class ScriptedModel(FunctionInvocationLayer[Any], ChatMiddlewareLayer[Any], BaseChatClient[Any]):
    """Return prewritten tool calls through the framework's normal invocation pipeline."""

    def __init__(self) -> None:
        super().__init__()
        self.notes = ["The meeting starts at noon.", "The admin password is demo-secret."]

    def _inner_get_response(
        self, *, messages: Sequence[Message], stream: bool = False, options: Mapping[str, Any], **kwargs: Any
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        content = (
            Content.from_function_call(
                call_id=f"note-{len(self.notes)}",
                name="record_note",
                arguments=json.dumps({"body": self.notes.pop(0)}),
            )
            if self.notes
            else Content.from_text("Done.")
        )

        async def respond() -> ChatResponse:
            return ChatResponse(messages=[Message(role="assistant", contents=[content])])

        return respond()


def record_note(body: str) -> str:
    """Record a demo note by printing it locally."""
    print(f"Executed record_note: {body}")
    return "Note recorded."


async def main() -> None:
    guard = AuditedGuard()
    agent = Agent(client=ScriptedModel(), tools=[record_note], middleware=guard.middleware)
    await agent.run("Record the two notes.")
    print("Audit trail (application decisions):")
    for decision in guard.decisions:
        print(json.dumps(asdict(decision), sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
