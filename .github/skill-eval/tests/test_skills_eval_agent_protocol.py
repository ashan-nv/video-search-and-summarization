# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the skills-eval agent's terminal outcome protocol."""

from __future__ import annotations

from enum import Enum
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "skills_eval_agent",
    Path(__file__).resolve().parents[1] / "skills_eval_agent.py",
)
skills_eval_agent = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(skills_eval_agent)


def test_contract_suite_uses_requested_python_runtime() -> None:
    assert (
        skills_eval_agent.CLAUDE_AGENT_SDK_REQUIREMENT
        == "claude-agent-sdk==0.2.128"
    )
    expected = os.environ.get("SKILL_EVAL_EXPECTED_PYTHON_VERSION")
    if expected is not None:
        actual = f"{sys.version_info.major}.{sys.version_info.minor}"
        assert actual == expected


def test_agent_rejects_an_unpinned_python_minor() -> None:
    with mock.patch.object(skills_eval_agent.sys, "version_info", (3, 12, 9)):
        skills_eval_agent._require_supported_python()

    with mock.patch.object(skills_eval_agent.sys, "version_info", (3, 10, 19)):
        try:
            skills_eval_agent._require_supported_python()
        except RuntimeError as exc:
            assert "requires Python 3.12.x; found 3.10" in str(exc)
        else:
            raise AssertionError("Python 3.10 unexpectedly passed the runtime guard")


def _exit_code(*blocks: str) -> int:
    return skills_eval_agent._evaluate_terminal_marker(list(blocks))[0]


def test_full_positive_pass_succeeds() -> None:
    assert _exit_code("DONE: 3/3 specs passed; 0 blockers") == 0
    assert _exit_code("DONE: 1/1 spec passed") == 0


def test_zero_of_one_timeout_fails() -> None:
    assert (
        _exit_code(
            "DONE: 0/1 specs passed; step-1 timed out at 7800s "
            "(reward=missing), steps 2-7 skipped"
        )
        == skills_eval_agent._EVAL_FAILURE_EXIT_CODE
    )


def test_partial_pass_fails() -> None:
    assert (
        _exit_code("DONE: 2/3 specs passed; 1 spec failed")
        == skills_eval_agent._EVAL_FAILURE_EXIT_CODE
    )


def test_malformed_done_fails_closed() -> None:
    assert (
        _exit_code("DONE: all specs passed")
        == skills_eval_agent._PROTOCOL_FAILURE_EXIT_CODE
    )
    assert (
        _exit_code("DONE: 0/0 specs passed")
        == skills_eval_agent._PROTOCOL_FAILURE_EXIT_CODE
    )
    assert (
        _exit_code("DONE: 2/1 specs passed")
        == skills_eval_agent._PROTOCOL_FAILURE_EXIT_CODE
    )


def test_only_final_nonempty_line_is_the_terminal_marker() -> None:
    assert (
        _exit_code(
            "DONE: 3/3 specs passed; stale earlier marker",
            "The timeout result supersedes the earlier summary.\n"
            "DONE: 0/1 specs passed; timed out",
        )
        == skills_eval_agent._EVAL_FAILURE_EXIT_CODE
    )
    assert (
        _exit_code("DONE: 1/1 specs passed\nTrailing prose")
        == skills_eval_agent._PROTOCOL_FAILURE_EXIT_CODE
    )
    assert (
        _exit_code("The expected format is DONE: N/N specs passed")
        == skills_eval_agent._PROTOCOL_FAILURE_EXIT_CODE
    )
    assert (
        _exit_code(" DONE: 1/1 specs passed")
        == skills_eval_agent._PROTOCOL_FAILURE_EXIT_CODE
    )


def test_fenced_done_marker_from_harbor_pass_is_accepted() -> None:
    """Harbor 1.0 plus a fenced DONE: must be exit 0 (run 32225077286)."""
    assert (
        _exit_code(
            "Comment posted at [PR #1751 comment 5339053947]"
            "(https://github.com/NVIDIA-AI-Blueprints/"
            "video-search-and-summarization/pull/1751"
            "#issuecomment-5339053947).\n"
            "\n"
            "Summary of the eval:\n"
            "- **Verifier**: 7/7 checks passed, reward = 1.0\n"
            "\n"
            "```\n"
            "DONE: 1/1 specs passed; 0 blockers\n"
            "```"
        )
        == 0
    )
    assert _exit_code("```\nDONE: 1/1 spec passed\n```") == 0
    assert _exit_code("```text\nDONE: 1/1 specs passed; 0 blockers\n```") == 0
    assert (
        _exit_code("```\nDONE: 1/1 specs passed; 0 blockers\n```\nTrailing prose")
        == skills_eval_agent._PROTOCOL_FAILURE_EXIT_CODE
    )


def test_inline_backtick_done_marker_from_harbor_pass_is_accepted() -> None:
    """Harbor 1.0 plus a tick-wrapped DONE: must be exit 0 (run 32229635259)."""
    assert (
        _exit_code(
            "Successfully evaluated `skills/vss-deploy-profile/evals/base.json` "
            "on `RTXPRO6000BW`:\n"
            "\n"
            "- **Reward: 1.0 (7/7 checks passed)**\n"
            "\n"
            "`DONE: 1/1 specs passed; base@RTXPRO6000BW reward=1.0 "
            "(7/7 checks) in 59m 07s`"
        )
        == 0
    )
    assert _exit_code("`DONE: 1/1 spec passed`") == 0
    assert (
        _exit_code("`DONE: 1/1 specs passed; 0 blockers`\nTrailing prose")
        == skills_eval_agent._PROTOCOL_FAILURE_EXIT_CODE
    )


def test_blocked_fails_the_github_job() -> None:
    assert _exit_code("BLOCKED: pool exhausted for RTXPRO6000BW") == (
        skills_eval_agent._BLOCKED_EXIT_CODE
    )
    assert _exit_code("BLOCKED: docker daemon unreachable") == (
        skills_eval_agent._BLOCKED_EXIT_CODE
    )


def test_blocked_requires_a_nonempty_reason() -> None:
    assert _exit_code("BLOCKED:") == skills_eval_agent._PROTOCOL_FAILURE_EXIT_CODE
    assert _exit_code("BLOCKED:   ") == skills_eval_agent._PROTOCOL_FAILURE_EXIT_CODE


def test_result_message_errors_and_both_max_turn_schemas_are_detected() -> None:
    class TerminalReason(Enum):
        MAX_TURNS = "max_turns"

    assert skills_eval_agent._result_message_state(
        SimpleNamespace(stop_reason="max_turns", is_error=True)
    ) == (True, True)
    assert skills_eval_agent._result_message_state(
        SimpleNamespace(terminal_reason=TerminalReason.MAX_TURNS, is_error=True)
    ) == (True, True)
    assert skills_eval_agent._result_message_state(
        SimpleNamespace(subtype="error_max_turns", is_error=True)
    ) == (True, True)
    assert skills_eval_agent._result_message_state(
        SimpleNamespace(stop_reason="end_turn", is_error=True)
    ) == (False, True)
    assert skills_eval_agent._result_message_state(
        SimpleNamespace(terminal_reason="success", is_error=False)
    ) == (False, False)


def test_bash_watchdog_does_not_preempt_workflow_job() -> None:
    with mock.patch.dict(
        os.environ,
        {
            "BASH_DEFAULT_TIMEOUT_MS": "60000",
            "BASH_MAX_TIMEOUT_MS": "not-an-integer",
        },
        clear=True,
    ):
        skills_eval_agent._set_bash_timeouts()
        expected = str(skills_eval_agent.BASH_FOREGROUND_TIMEOUT_MS)
        assert expected == "50400000"
        assert os.environ["BASH_DEFAULT_TIMEOUT_MS"] == expected
        assert os.environ["BASH_MAX_TIMEOUT_MS"] == expected


def test_work_deadlines_reserve_verdict_and_job_reporting_time() -> None:
    with mock.patch.dict(os.environ, {}, clear=True):
        before = time.monotonic()
        skills_eval_agent._set_work_deadline()
        after = time.monotonic()
        deadline = float(
            os.environ[skills_eval_agent.SKILL_EVAL_WORK_DEADLINE_ENV]
        )
        harbor_deadline = float(
            os.environ[skills_eval_agent.SKILL_EVAL_HARBOR_DEADLINE_ENV]
        )

    assert before + skills_eval_agent.SKILL_EVAL_WORK_BUDGET_SEC <= deadline
    assert deadline <= after + skills_eval_agent.SKILL_EVAL_WORK_BUDGET_SEC
    assert (
        deadline - harbor_deadline
        == skills_eval_agent.SKILL_EVAL_AGENT_VERDICT_RESERVE_SEC
    )


def test_sdk_session_is_cancelled_at_the_reserved_work_deadline() -> None:
    # Exercise the real asyncio cancellation boundary in a child process. This
    # avoids sharing an event-loop lifecycle with IsolatedAsyncioTestCase tests
    # from the Brev transport suite when pytest collects both modules.
    module_path = Path(skills_eval_agent.__file__).resolve()
    script = f"""
import asyncio
import importlib.util
import os
import time

spec = importlib.util.spec_from_file_location("skills_eval_agent_child", {str(module_path)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

async def never_finishes():
    await asyncio.Event().wait()

module.run_agent = never_finishes
os.environ[module.SKILL_EVAL_WORK_DEADLINE_ENV] = str(time.monotonic() + 0.01)
try:
    asyncio.run(module._run_agent_with_work_deadline())
except module.WorkDeadlineExceeded:
    raise SystemExit(0)
raise SystemExit("agent session outlived its work deadline")
"""
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
