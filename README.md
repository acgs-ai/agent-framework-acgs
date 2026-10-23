# agent-framework-acgs

[![CI](https://github.com/acgs-ai/agent-framework-acgs/actions/workflows/ci.yml/badge.svg)](https://github.com/acgs-ai/agent-framework-acgs/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agent-framework-acgs.svg)](https://pypi.org/project/agent-framework-acgs/)
[![Python](https://img.shields.io/pypi/pyversions/agent-framework-acgs.svg)](https://pypi.org/project/agent-framework-acgs/)

`agent-framework-acgs` checks [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) function
calls against [ACGS](https://pypi.org/project/acgs-lite/) constitution rules before the function runs. It uses two
documented framework mechanisms:

- [function middleware](https://learn.microsoft.com/en-us/agent-framework/agents/middleware/), which runs right
  before each function call, including a call that was approved;
- [tool approval](https://learn.microsoft.com/en-us/agent-framework/agents/tools/tool-approval), so calls that need
  review pause the run as `function_approval_request` items that you approve or reject in the same session.

What happens to a call:

- Calls that match no blocking rule run unchanged.
- Calls that match a blocking rule do not run. The model gets the reason as the function result and can continue,
  or the run ends with `ACGSToolCallDenied` if you pass `on_deny="raise"`.
- Calls that only match review rules pause the run for approval and run only once approved. They can also be
  rejected.
- A blocking rule always wins. Nobody is asked to approve a blocked call, and an approved call is checked again
  and not run if it is blocked.
- Any error while evaluating a call denies it (fail-closed).
- A run that offers tools the middleware cannot check (hosted tools, declaration-only tools, tools the caller
  runs) fails with `UngovernedToolsError` before the model is called.

The package is a thin adapter over the public `acgs-lite` API.

> [!NOTE]
> This is a community package. It is not part of Microsoft Agent Framework and is not maintained, reviewed or
> endorsed by Microsoft.

## Installation

```bash
pip install agent-framework-acgs
pip install "agent-framework-acgs[mcp]"  # for local MCP tools
```

Requires Python 3.10 or later, `agent-framework-core>=1.20.0,<2` and `acgs-lite>=3.0.1,<4`. The `mcp` extra adds
`mcp` and `tenacity`, which Agent Framework's MCP tools need.

## Usage

`guard.middleware` is a list with a chat middleware (`guard.tool_check`, which checks the tools a run offers) and a
function middleware (`guard.function_middleware`, which checks each call). Pass it wherever Agent Framework takes
middleware: to the agent, to one run, or to the chat client.

```python
from agent_framework import Agent

from agent_framework_acgs import ACGSGuard


def send_email(to: str, body: str) -> str:
    """Send an email."""
    return f"sent to {to}"


guard = ACGSGuard()
# client is any Agent Framework chat client, for example OpenAIChatClient() from agent-framework-openai.
agent = Agent(client=client, tools=[send_email], middleware=guard.middleware)

# For one run only:
# await agent.run("...", middleware=guard.middleware)
```

## Quickstart

`examples/quickstart.py` runs offline. A small scripted chat client built on the framework's own
`FunctionInvocationLayer` and `ChatMiddlewareLayer` stands in for a model:

```python
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
```

```text
email-1: Tool call 'send_email' was not executed. Blocked by ACGS rule ACGS-006 (Agents must not expose sensitive data in responses).
weather-1: Sunny in Paris
paused: issue_refund: Human review required by ACGS rule REFUND-REVIEW (Refunds need human approval).
refund-1: refunded 20.0 for A-17
```

## How a call is evaluated

1. The function name and the call's arguments are rendered as text by `format_tool_call`:

   ```python
   from agent_framework_acgs import format_tool_call

   print(format_tool_call("update_config", {"service": "billing", "change": "disable\nlogging", "dryRun": False}))
   ```

   ```text
   tool: update_config
   arguments:
   change: disable logging
   dryRun: false
   service: billing
   normalized: update config | dry run
   ```

   Each argument leaf is one `path: value` line (nested values use `a.b[0].c` paths), so both argument names and
   values are visible to rules. Whitespace inside values is collapsed so phrases split across lines still match.
   Identifiers written in `snake_case`, `kebab-case` or `camelCase` (the function name, argument names, and
   single-token string values) are repeated as plain words on the `normalized:` line, so the keyword
   `skip audit` also matches a function called `skip_audit`. Nothing is truncated.
   Pass `tool_call_formatter=` to use a different representation.

   The arguments are the ones the function will receive (`FunctionInvocationContext.arguments`): parsed against
   the function's input model, without defaults the model did not send.

2. The text is validated with `acgs_lite.GovernanceEngine.validate` against the constitution
   (`Constitution.default()` unless you pass one).

3. The result decides what happens to the call:

   | ACGS outcome | Result |
   |---|---|
   | No blocking rule matched (warn-only rules may match) | The function runs |
   | Only `require_human_review` / `escalate_to_senior` rules matched | Review (see below) |
   | Any `block`, `block_and_notify` or `halt_and_alert` rule matched | Denied |
   | A rule ID not found in the constitution, or an invalid result without a rule | Denied |
   | Any exception while reading or evaluating the call | Denied (fail-closed) |

   A matched blocking rule is never downgraded to review because a review rule matched as well.
   The denial reason then lists only the blocking rules after "Blocked by" and names the matched review
   rules separately.

A denied call is not executed. The middleware sets `context.result` to
`"Tool call '<name>' was not executed. <reason>"` and does not call the rest of the pipeline, so the model gets the
reason as the function result. The reason names the matched rules but never repeats argument values. With
`on_deny="raise"`, the middleware raises `ACGSToolCallDenied` (a `MiddlewareFailure`, which Agent Framework
passes to the caller); its `decision` attribute holds the decision.

Every decision is stored in `context.metadata["acgs_decision"]` (`DECISION_METADATA_KEY`) for middleware placed
before the guard. `ACGSGuard.evaluate(tool_name, args)` returns the `GovernanceDecision` without running
anything.

## Review

A call that needs review pauses the run. The middleware sets the call's result to a `function_approval_request`,
which the framework passes through to drive its approval flow (as described for `result` in the
`FunctionInvocationContext` docstring). The request appears in `response.user_input_requests`; answer it in the
same session:

```python
from agent_framework import Agent, AgentResponse, Message


async def run_with_review(agent: Agent, prompt: str) -> AgentResponse:
    session = agent.create_session()
    response = await agent.run(prompt, session=session)
    while response.user_input_requests:
        answers = []
        for request in response.user_input_requests:
            print(request.function_call.name, request.additional_properties.get("reason"))
            answers.append(request.to_function_approval_response(approved=True))  # or approved=False
        response = await agent.run(Message(role="user", contents=answers), session=session)
    return response
```

When the approved call is replayed, the middleware evaluates it again. It runs only if it still needs review and
the framework passed an approval for it; if it is blocked by then, it is denied.

Things to know:

- Review needs an `AgentSession`. The framework binds approval responses to the requests it recorded in the session,
  so an approval without the session, a forged approval, or a replayed approval does not run a call, and an
  approval with edited arguments does not run the edited call. `guard.tool_check` raises `ACGSConfigurationError` if the chat client sets
  `disable_approval_response_binding` (use `on_review="deny"` there).
- Each request carries `additional_properties["reason"]` and `additional_properties["acgs"]`
  (`APPROVAL_PROPERTIES_KEY`) with `verdict`, `rule_ids` and `warning_rule_ids`.
- A rejected call does not run; the model gets the framework's rejection message.
- Streamed runs work the same; the requests are on the final response. Sessions saved with `session.to_dict()` and
  restored with `AgentSession.from_dict(...)` can be resumed.
- For a function with `approval_mode="always_require"`, the framework asks first and one approval covers both;
  a blocked call is denied after approval.
- Standing approval rules of `ToolApprovalMiddleware` (`create_always_approve_tool_response`) also answer ACGS
  review requests: later review calls of that tool in the session run without asking. Blocked calls are still
  denied.
- An agent used as a tool (`Agent.as_tool`) cannot ask for approval through the outer run. A call inside it that
  needs review does not run, and the outer model gets an error result. Use `on_review="deny"` for such agents.
- Use `on_review="deny"` for runs nobody can approve.

## What is covered

| Tool | Checked | How |
|---|---|---|
| Function tools (plain functions, `@tool`, `FunctionTool(...)`) | Yes | Function middleware |
| Local MCP tools (`MCPStdioTool`, `MCPStreamableHTTPTool`, `MCPWebsocketTool`) | Yes | The framework calls them as function tools |
| `Agent.as_tool(...)` | The call to the agent tool, yes | Pass the guard to the inner agent too to check its calls |
| Tools added during a run (`FunctionInvocationContext.add_tools`) | Function tools, yes | Others are refused before the next model call |
| Hosted tools (provider tools given as dicts or provider objects, such as hosted web search, code interpreter or hosted MCP) | No | The provider runs them; refused |
| Declaration-only function tools (no implementation) | No | The caller runs them; refused |
| `function_invocation_configuration["additional_tools"]` | No | The caller runs them; refused |
| Any tool when function invocation is off (`enabled=False`) or the client has no `FunctionInvocationLayer` | No | The caller runs them; refused |

Refused means `guard.tool_check` raises `UngovernedToolsError` before the model is called. Pass
`allow_ungoverned=True` to run such tools with a logged warning instead; their calls are not checked.

`guard.tool_check` is a chat middleware, so it runs only on chat clients built with `ChatMiddlewareLayer`. On a
client without it, the function middleware still checks every call and raises `UngovernedToolsError` when a
function is called in a run that offers ungoverned tools, but by then the model has already seen those tools.

## Middleware order

Function middleware runs in the order client, agent, run, and in list order within each. Middleware placed after
the guard runs after the check, so a middleware there that changes `context.arguments` changes them unchecked.
Put `guard.middleware` last, at the innermost level you use.

## Custom constitutions

```python
from acgs_lite import Constitution, Rule, Severity, ViolationAction

from agent_framework_acgs import ACGSGuard

constitution = Constitution.from_rules(
    [
        Rule(
            id="NO-DESTRUCTIVE-SQL",
            text="No destructive SQL",
            severity=Severity.CRITICAL,
            keywords=["drop table", "truncate table"],
        ),
        Rule(
            id="REFUND-REVIEW",
            text="Refunds need approval",
            severity=Severity.HIGH,
            keywords=["refund"],
            workflow_action=ViolationAction.REQUIRE_HUMAN_REVIEW,
        ),
    ],
    name="payments",
)
guard = ACGSGuard(constitution=constitution, agent_id="payments-app")
```

`Constitution.from_yaml("rules.yaml")` works as well. To enforce two constitutions, add two guards; a call runs
only if both allow it.

## Options

| Argument | Default | Meaning |
|---|---|---|
| `constitution` | `Constitution.default()` | Rules to enforce |
| `agent_id` | `"agent-framework"` | Agent identifier recorded by ACGS |
| `on_review` | `"approve"` | `"approve"` (pause for approval) or `"deny"` |
| `on_deny` | `"reject"` | `"reject"` (function result with the reason) or `"raise"` (`ACGSToolCallDenied`) |
| `allow_ungoverned` | `False` | Run tools the middleware cannot check, with a logged warning, instead of raising |
| `tool_call_formatter` | `format_tool_call` | `(tool_name, args) -> str` used as the evaluated text |

## Limitations

- Only tools listed as checked in [What is covered](#what-is-covered). User input, model output, and function
  results are not checked; other Agent Framework middleware can handle those.
- With `on_deny="raise"`, other calls from the same model turn may already have run: with concurrent invocation
  (the default), allowed calls can run alongside the denied one; with `allow_concurrent_invocation=False`, earlier
  calls ran and later ones do not start.
- Defaults the model did not send are not evaluated, and arguments the function does not declare are dropped by
  the framework before the check (they are not passed to the function either).
- Middleware placed after the guard can change the arguments after the check (see
  [Middleware order](#middleware-order)).
- Tested with `Agent.run` streamed and not, parallel calls in one model turn, approval, rejection and resume
  (including a serialized session), forged, edited, replayed and session-less approvals, `approval_mode`,
  `ToolApprovalMiddleware`, client-, agent- and run-level middleware, `Agent.as_tool`, `add_tools`, and a local
  stdio MCP server, against agent-framework-core 1.20.0 and the latest release. Workflows, hosted agent services
  and declarative agents are not tested.
- `Constitution.default()` focuses on governance integrity (self-approval, skipping audits, privilege escalation)
  and on secrets and personal data (`password`, `api_key`, `sk-...` and `ghp_...` tokens, US SSN
  patterns). It does **not** cover destructive operations such as `rm -rf` or `DROP TABLE`. Write rules for the
  tools you expose. `tests/test_default_constitution.py` lists what the default rules do and do not catch.
- Keyword rules in acgs-lite 3.0.1 match literal, case-insensitive substrings. They do not understand negation
  ("never share the password" matches `password`) and do not treat `-`, `_` and spaces as equivalent
  (`api-key` does not match the keyword `api_key`).
- Because argument names are part of the evaluated text, a function with a parameter named `password` is denied
  under the default constitution whenever it is called.
- Agent Framework releases often. The adapter allows `agent-framework-core<2` and is tested against the oldest
  supported and the latest release. agent-framework-core 1.20.0 is the minimum because earlier releases run an
  approved call even when the approval came without a session.

## Development

```bash
uv sync --group dev        # or use hatch: hatch run prepare
hatch run format           # ruff format + ruff check --fix
hatch run lint             # ruff check + ruff format --check
hatch run typecheck        # mypy --strict
hatch run cov              # pytest with a 100% branch-coverage gate, network disabled
hatch run min:cov          # the same tests against agent-framework-core 1.20.0, acgs-lite 3.0.1 and mcp 1.24.0
pre-commit install         # format, lint, type check and tests on commit; commitizen on commit messages
```

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/); `cz bump` updates the version
and the changelog.

## License

Apache-2.0. See [LICENSE](LICENSE).
