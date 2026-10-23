# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""ACGS governance for Microsoft Agent Framework function tools: function middleware plus a chat-level tool check."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal

from acgs_lite import Constitution
from agent_framework import (
    ChatContext,
    ChatMiddleware,
    Content,
    FunctionInvocationContext,
    FunctionMiddleware,
    FunctionTool,
    MCPStdioTool,
    MCPStreamableHTTPTool,
    MCPWebsocketTool,
    MiddlewareFailure,
    normalize_tools,
)
from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from agent_framework_acgs._governance import GovernanceDecision, ToolCallFormatter, ToolCallGovernor, Verdict

logger = logging.getLogger(__name__)

DECISION_METADATA_KEY = "acgs_decision"
"""Key of the ``GovernanceDecision`` the function middleware stores in ``FunctionInvocationContext.metadata``."""

APPROVAL_PROPERTIES_KEY = "acgs"
"""Key of the ACGS details in the ``additional_properties`` of an ACGS approval request."""

# Local MCP tools are expanded into function tools whose calls go through function middleware.
_MCP_TOOLS = (MCPStdioTool, MCPStreamableHTTPTool, MCPWebsocketTool)
_EVALUATION_FAILED = "ACGS governance could not evaluate this tool call, so it was denied."
_APPROVAL_REQUEST_FAILED = "The approval request failed, so it was denied."
_CALLER_RUNS_ALL = (
    "all offered tools (function invocation is off or missing on the chat client, so the caller runs them)"
)


class ACGSConfigurationError(MiddlewareFailure):
    """Raised during a run when ACGS cannot enforce its rules with the current configuration.

    It subclasses ``MiddlewareFailure``, so the framework stops the run and raises it to the caller
    instead of turning it into a tool result.
    """


class UngovernedToolsError(ACGSConfigurationError):
    """Raised when a run offers tools whose calls ACGS cannot check (see ``ACGSGuard.ungoverned_tools``)."""


class ACGSToolCallDenied(MiddlewareFailure):
    """Raised for a denied call when the guard uses ``on_deny="raise"``. It ends the run."""

    def __init__(self, decision: GovernanceDecision) -> None:
        """Create the error.

        Args:
            decision: The decision that denied the call.
        """
        super().__init__(decision.reason)
        self.decision = decision
        """The ACGS decision for the call."""


def _arguments(arguments: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(arguments, BaseModel):
        return arguments.model_dump(mode="json")
    return dict(arguments)


def _is_approved(approval_response: object) -> bool:
    return (
        isinstance(approval_response, Content)
        and approval_response.type == "function_approval_response"
        and approval_response.approved is True
    )


def _describe_ungoverned(tool: object) -> str | None:
    if isinstance(tool, FunctionTool):
        return f"declaration-only function tool '{tool.name}'" if tool.declaration_only else None
    if isinstance(tool, _MCP_TOOLS):
        return None
    if isinstance(tool, Mapping):
        kind = tool.get("type") or tool.get("name") or "unknown"
        return f"hosted tool '{kind}'"
    return f"tool object of type {type(tool).__name__}"


class ACGSFunctionMiddleware(FunctionMiddleware):
    """Function middleware that checks each function call with an ``ACGSGuard`` before the function runs.

    Create it through ``ACGSGuard`` (``guard.function_middleware`` or ``guard.middleware``).
    """

    def __init__(self, guard: ACGSGuard) -> None:
        """Create the middleware.

        Args:
            guard: The guard whose rules and options apply.
        """
        self.guard = guard

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        """Run the function only if ACGS allows the call, or a human approved a call that needs review.

        Args:
            context: The function invocation context.
            call_next: Runs the rest of the pipeline and the function.

        Raises:
            UngovernedToolsError: If the run offers tools ACGS cannot check and ``allow_ungoverned`` is off.
            ACGSToolCallDenied: If the call is denied and ``on_deny="raise"``.
        """
        guard = self.guard
        guard.check_tools(context.tools)
        name = context.function.name
        try:
            decision = guard.evaluate(name, _arguments(context.arguments))
        except Exception as error:
            decision = GovernanceDecision(
                verdict=Verdict.DENY,
                tool_name=name,
                reason=_EVALUATION_FAILED,
                error=f"{type(error).__name__}: {error}",
            )
        context.metadata[DECISION_METADATA_KEY] = decision
        if decision.verdict is Verdict.ALLOW:
            await call_next()
            return
        if decision.verdict is Verdict.REVIEW and guard.on_review == "approve":
            if _is_approved(context.metadata.get("approval_response")):
                await call_next()
                return
            try:
                context.result = self._approval_request(context, decision)
            except Exception as error:
                decision = GovernanceDecision(
                    verdict=Verdict.DENY,
                    tool_name=name,
                    reason=f"{decision.reason} {_APPROVAL_REQUEST_FAILED}",
                    rule_ids=decision.rule_ids,
                    warning_rule_ids=decision.warning_rule_ids,
                    error=f"{type(error).__name__}: {error}",
                )
                context.metadata[DECISION_METADATA_KEY] = decision
            else:
                return
        if guard.on_deny == "raise":
            raise ACGSToolCallDenied(decision)
        context.result = f"Tool call '{name}' was not executed. {decision.reason}"

    @staticmethod
    def _approval_request(context: FunctionInvocationContext, decision: GovernanceDecision) -> Content:
        call_id = context.metadata["call_id"]
        occurrence_id = context.metadata.get("function_call_occurrence_id") or call_id
        function_call = Content.from_function_call(
            call_id=call_id,
            name=context.function.name,
            arguments=to_jsonable_python(_arguments(context.arguments)),
            id=occurrence_id,
        )
        return Content.from_function_approval_request(
            id=occurrence_id,
            function_call=function_call,
            additional_properties={
                "reason": decision.reason,
                APPROVAL_PROPERTIES_KEY: {
                    "verdict": decision.verdict.value,
                    "rule_ids": list(decision.rule_ids),
                    "warning_rule_ids": list(decision.warning_rule_ids),
                },
            },
        )


class ACGSToolCheck(ChatMiddleware):
    """Chat middleware that stops a run before the model is called if it offers tools ACGS cannot check.

    Create it through ``ACGSGuard`` (``guard.tool_check`` or ``guard.middleware``).
    """

    def __init__(self, guard: ACGSGuard) -> None:
        """Create the middleware.

        Args:
            guard: The guard whose options apply.
        """
        self.guard = guard

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        """Check the offered tools and the chat client's function invocation settings, then call the model.

        Args:
            context: The chat context.
            call_next: Calls the model.

        Raises:
            ACGSConfigurationError: If ``on_review="approve"`` and the client does not bind approval responses
                to recorded requests.
            UngovernedToolsError: If the run offers tools ACGS cannot check and ``allow_ungoverned`` is off.
        """
        guard = self.guard
        config: Mapping[str, Any] = getattr(context.client, "function_invocation_configuration", None) or {}
        if guard.on_review == "approve" and config.get("disable_approval_response_binding"):
            msg = (
                "ACGS review needs approval responses bound to recorded requests, but the chat client sets "
                "disable_approval_response_binding. Turn it off or use on_review='deny'."
            )
            raise ACGSConfigurationError(msg)
        tools = normalize_tools(context.options.get("tools") if context.options else None)
        if tools and not config.get("enabled", False):
            guard.report_ungoverned([_CALLER_RUNS_ALL])
        additional = [
            f"additional tool '{tool.name}' (the caller runs it)" for tool in config.get("additional_tools") or ()
        ]
        if additional:
            guard.report_ungoverned(additional)
        guard.check_tools(tools)
        await call_next()


class ACGSGuard:
    """Check Microsoft Agent Framework function calls against an ACGS constitution before the function runs.

    Add ``guard.middleware`` to an agent (or a chat client, or one run). It contains:

    - ``function_middleware`` (``ACGSFunctionMiddleware``), which checks every call of a function tool,
      including local MCP tools. Allowed calls run. Blocked calls do not run: the reason becomes the
      call's result, which the model reads, or with ``on_deny="raise"`` the run ends with
      ``ACGSToolCallDenied``. Calls that only match review rules pause the run with a native
      ``function_approval_request`` in ``response.user_input_requests``. When the approval is sent
      back in the same session, the framework replays the call and the guard checks it again.
    - ``tool_check`` (``ACGSToolCheck``), which stops the run before the model is called when it
      offers tools that the function middleware never sees: hosted tools, declaration-only function
      tools, the chat client's ``additional_tools``, or any tool when the client does not run the
      tool loop itself.

    A blocking rule always wins over review rules, and any error while evaluating a call denies it.

    Example:
        >>> from agent_framework_acgs import ACGSGuard
        >>> guard = ACGSGuard()
        >>> guard.evaluate("send_email", {"to": "a@example.com", "body": "The password is hunter2"}).verdict
        <Verdict.DENY: 'deny'>
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        constitution: Constitution | None = None,
        agent_id: str = "agent-framework",
        on_review: Literal["approve", "deny"] = "approve",
        on_deny: Literal["reject", "raise"] = "reject",
        allow_ungoverned: bool = False,
        tool_call_formatter: ToolCallFormatter | None = None,
    ) -> None:
        """Create the guard.

        Args:
            constitution: The ACGS constitution to enforce. Defaults to
                ``acgs_lite.Constitution.default()``.
            agent_id: Agent identifier recorded by ACGS for each validation.
            on_review: What to do when only review rules match. ``"approve"`` asks for approval through
                the framework's tool approval flow. ``"deny"`` denies the call.
            on_deny: What to do with a denied call. ``"reject"`` skips the function and sends the reason to
                the model as the call's result. ``"raise"`` ends the run with ``ACGSToolCallDenied``.
            allow_ungoverned: Log a warning instead of stopping the run when it offers tools ACGS cannot check.
            tool_call_formatter: Optional function ``(tool_name, args) -> str`` that replaces
                ``format_tool_call``.

        Raises:
            ValueError: If ``on_review`` or ``on_deny`` is invalid.
        """
        if on_review not in ("approve", "deny"):
            msg = f"on_review must be 'approve' or 'deny', got {on_review!r}"
            raise ValueError(msg)
        if on_deny not in ("reject", "raise"):
            msg = f"on_deny must be 'reject' or 'raise', got {on_deny!r}"
            raise ValueError(msg)
        self._governor = ToolCallGovernor(constitution, agent_id=agent_id, tool_call_formatter=tool_call_formatter)
        self.constitution: Constitution = self._governor.constitution
        self.agent_id = agent_id
        self.on_review = on_review
        self.on_deny = on_deny
        self.allow_ungoverned = allow_ungoverned
        self.function_middleware = ACGSFunctionMiddleware(self)
        """The function middleware."""
        self.tool_check = ACGSToolCheck(self)
        """The chat middleware that refuses tools ACGS cannot check."""

    def __repr__(self) -> str:
        """Show the options, not the constitution."""
        return (
            f"ACGSGuard(agent_id={self.agent_id!r}, on_review={self.on_review!r}, on_deny={self.on_deny!r}, "
            f"allow_ungoverned={self.allow_ungoverned!r})"
        )

    @property
    def middleware(self) -> list[ACGSToolCheck | ACGSFunctionMiddleware]:
        """Both middleware, as a new list to pass as ``middleware=``."""
        return [self.tool_check, self.function_middleware]

    def evaluate(self, tool_name: str, arguments: Any) -> GovernanceDecision:
        """Evaluate a tool call without running an agent. Never raises.

        Args:
            tool_name: Name of the tool.
            arguments: The tool arguments.

        Returns:
            The governance decision.
        """
        return self._governor.evaluate(tool_name, arguments)

    @staticmethod
    def ungoverned_tools(tools: Any) -> list[str]:
        """Describe the tools whose calls the function middleware never sees.

        Function tools with an implementation and local MCP tools are governed. Hosted tools
        (mappings), other tool objects and declaration-only function tools are not.

        Args:
            tools: A tool, a callable, or a sequence of them, as accepted by ``tools=``.

        Returns:
            One description per ungoverned tool, empty if every tool is governed.
        """
        return [d for d in map(_describe_ungoverned, normalize_tools(tools)) if d is not None]

    def check_tools(self, tools: Sequence[Any] | None) -> None:
        """Report ungoverned tools (see ``report_ungoverned``).

        Args:
            tools: The tools offered to the model.
        """
        ungoverned = self.ungoverned_tools(tools)
        if ungoverned:
            self.report_ungoverned(ungoverned)

    def report_ungoverned(self, descriptions: Sequence[str]) -> None:
        """Raise ``UngovernedToolsError``, or log a warning when ``allow_ungoverned`` is set.

        Args:
            descriptions: What ACGS cannot check.

        Raises:
            UngovernedToolsError: Unless ``allow_ungoverned`` is set.
        """
        listed = "; ".join(descriptions)
        if self.allow_ungoverned:
            logger.warning("ACGS does not check calls of: %s (allow_ungoverned is set).", listed)
            return
        msg = (
            f"ACGS cannot check calls of: {listed}. Remove them, or pass allow_ungoverned=True to run them "
            "without ACGS checks."
        )
        raise UngovernedToolsError(msg)
