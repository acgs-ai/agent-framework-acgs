# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
import agent_framework_acgs


def test_public_api() -> None:
    assert sorted(agent_framework_acgs.__all__) == sorted(
        [
            "APPROVAL_PROPERTIES_KEY",
            "DECISION_METADATA_KEY",
            "ACGSConfigurationError",
            "ACGSFunctionMiddleware",
            "ACGSGuard",
            "ACGSToolCallDenied",
            "ACGSToolCheck",
            "GovernanceDecision",
            "ToolCallFormatter",
            "UngovernedToolsError",
            "Verdict",
            "format_tool_call",
        ]
    )
    for name in agent_framework_acgs.__all__:
        assert getattr(agent_framework_acgs, name) is not None
