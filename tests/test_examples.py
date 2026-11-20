# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""The README only shows code that runs, and the quickstart output it shows is the real output."""

from __future__ import annotations

import json
import os
import re
import runpy
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from agent_framework import Agent

from agent_framework_acgs import ACGSGuard
from tests.fakes import REFUND, ScriptedChatClient, Tools, call, review_rules

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
QUICKSTART = ROOT / "examples" / "quickstart.py"


def _blocks(language: str) -> list[str]:
    return [textwrap.dedent(block) for block in re.findall(rf"```{language}\n(.*?)```", README, re.DOTALL)]


def test_quickstart_output_matches_readme() -> None:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-W", "error", str(QUICKSTART)],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
        timeout=300,
    )
    [expected] = [block for block in _blocks("text") if block.startswith("email-1:")]
    assert completed.stdout == expected
    assert completed.stderr == ""


def test_readme_embeds_the_quickstart_verbatim() -> None:
    assert QUICKSTART.read_text(encoding="utf-8") in _blocks("python")


def test_deny_and_audit_runs_offline_without_api_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in list(os.environ):
        if key.endswith("API_KEY"):
            monkeypatch.delenv(key)
    # Register the example's import-time mutation so monkeypatch restores the original value.
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    # pytest-socket blocks network access for this in-process execution.
    runpy.run_path(str(ROOT / "examples" / "deny_and_audit.py"), run_name="__main__")
    execution, audit = capsys.readouterr().out.split("Audit trail (application decisions):\n")
    assert execution == "Executed record_note: The meeting starts at noon.\n"
    decisions = [json.loads(line) for line in audit.splitlines()]
    assert [decision["verdict"] for decision in decisions] == ["allow", "deny"]
    assert [decision["tool_name"] for decision in decisions] == ["record_note", "record_note"]
    assert decisions[0]["rule_ids"] == []
    assert decisions[1]["rule_ids"] == ["ACGS-006"]
    assert "Blocked by" in decisions[1]["reason"]
    assert all(decision["error"] is None for decision in decisions)
    assert "demo-secret" not in execution + audit


def test_readme_states_the_supported_versions() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for package in ("agent-framework-core", "acgs-lite"):
        [requirement] = re.findall(rf'"({package}>=[^"]+)"', pyproject)
        assert f"`{requirement}`" in README
        [floor] = re.findall(rf'"{package}==([^"]+)"', pyproject)
        assert requirement.startswith(f"{package}>={floor},")
    assert README.count("agent-framework-core 1.20.0") == 3


def test_every_readme_python_block_runs(capsys: pytest.CaptureFixture[str]) -> None:
    blocks = _blocks("python")
    assert len(blocks) == 5
    namespaces: list[dict[str, Any]] = []
    for block in blocks:
        namespace: dict[str, Any] = {"__name__": "readme", "client": ScriptedChatClient()}
        exec(compile(block, "<README>", "exec", dont_inherit=True), namespace)  # noqa: S102
        namespaces.append(namespace)
    assert "normalized: update config | dry run" in capsys.readouterr().out
    usage = namespaces[0]
    assert usage["agent"].middleware == usage["guard"].middleware
    assert isinstance(namespaces[-1]["guard"], ACGSGuard)


async def test_readme_review_loop_approves_and_resumes(capsys: pytest.CaptureFixture[str]) -> None:
    [block] = [block for block in _blocks("python") if "async def run_with_review" in block]
    namespace: dict[str, Any] = {"__name__": "readme"}
    exec(compile(block, "<README>", "exec", dont_inherit=True), namespace)  # noqa: S102
    tools = Tools()
    client = ScriptedChatClient(script=[[call("issue_refund", REFUND, "r1")], "Done."])
    governed = Agent(
        client=client, tools=[tools.issue_refund()], middleware=ACGSGuard(constitution=review_rules()).middleware
    )
    response = await namespace["run_with_review"](governed, "Refund order A-17")
    assert response.text == "Done."
    assert tools.ran == ["issue_refund"]
    assert "issue_refund Human review required by ACGS rule REFUND-REVIEW" in capsys.readouterr().out
