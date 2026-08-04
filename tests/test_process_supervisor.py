import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from goliath.agents.codex import build_codex_adapter
from goliath.agents.hermes import build_hermes_adapter
from goliath.agents.subprocess_adapter import SubprocessAgentAdapter
from goliath.agents.supervisor import ProcessSupervisor
from goliath.config import AgentConfig
from goliath.core.schemas import REQUIRED_FORBIDDEN_ACTIONS, ExecutionRequest


def request(tmp_path: Path, **overrides: object) -> ExecutionRequest:
    values: dict[str, object] = {
        "job_id": uuid4(),
        "agent": "fake",
        "task_type": "test",
        "objective": "Run fake executable",
        "workspace": tmp_path,
        "permissions": {"read_files"},
        "context_files": [],
        "forbidden_actions": set(REQUIRED_FORBIDDEN_ACTIONS),
        "timeout_seconds": 5,
        "max_output_chars": 100_000,
    }
    values.update(overrides)
    return ExecutionRequest.model_validate(values)


@pytest.mark.asyncio
async def test_success_captures_stdout_stderr_pid_and_structured_result(tmp_path: Path) -> None:
    adapter = SubprocessAgentAdapter(
        name="fake",
        command_template=[
            sys.executable,
            "-c",
            "import sys; print('{\"ok\": true}'); print('warning', file=sys.stderr)",
        ],
    )
    result = await adapter.run(request(tmp_path))
    assert result.succeeded
    assert result.process_id is not None
    assert result.exit_code == 0
    assert result.stdout == '{"ok": true}\n'
    assert result.stderr == "warning\n"
    assert result.structured_result == {"ok": True}


@pytest.mark.asyncio
async def test_failed_and_missing_executables_return_typed_failures(tmp_path: Path) -> None:
    failed = SubprocessAgentAdapter(
        name="failed",
        command_template=[sys.executable, "-c", "import sys; sys.exit(7)"],
    )
    failed_result = await failed.run(request(tmp_path))
    assert failed_result.exit_code == 7
    assert not failed_result.succeeded

    missing = SubprocessAgentAdapter(name="missing", command_template=["definitely-not-an-agent"])
    health = await missing.healthcheck()
    result = await missing.run(request(tmp_path))
    assert health.available is False
    assert result.exit_code is None
    assert result.failure_reason


@pytest.mark.asyncio
async def test_timeout_terminates_process(tmp_path: Path) -> None:
    adapter = SubprocessAgentAdapter(
        name="slow",
        command_template=[sys.executable, "-c", "import time; time.sleep(20)"],
        supervisor=ProcessSupervisor(cancellation_grace_seconds=0.1),
    )
    result = await adapter.run(request(tmp_path, timeout_seconds=1))
    assert result.timed_out
    assert result.exit_code is not None


@pytest.mark.asyncio
async def test_timeout_forces_kill_after_grace_period(tmp_path: Path) -> None:
    adapter = SubprocessAgentAdapter(
        name="stubborn",
        command_template=[
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(20)",
        ],
        supervisor=ProcessSupervisor(cancellation_grace_seconds=0.05),
    )
    result = await adapter.run(request(tmp_path, timeout_seconds=1))
    assert result.timed_out
    assert result.exit_code == -9


@pytest.mark.asyncio
async def test_cancellation_and_process_group_cleanup(tmp_path: Path) -> None:
    marker = tmp_path / "child-survived"
    child_code = f"import time, pathlib; time.sleep(1); pathlib.Path({str(marker)!r}).touch()"
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(20)"
    )
    supervisor = ProcessSupervisor(cancellation_grace_seconds=0.1)
    adapter = SubprocessAgentAdapter(
        name="group",
        command_template=[sys.executable, "-c", parent_code],
        supervisor=supervisor,
    )
    execution_request = request(tmp_path)
    running = asyncio.create_task(adapter.run(execution_request))
    await asyncio.sleep(0.2)
    assert await adapter.cancel(execution_request.job_id)
    result = await running
    await asyncio.sleep(1.1)
    assert result.cancelled
    assert not marker.exists()


@pytest.mark.asyncio
async def test_output_limits_record_truncation_metadata_without_deadlock(tmp_path: Path) -> None:
    adapter = SubprocessAgentAdapter(
        name="noisy",
        command_template=[
            sys.executable,
            "-c",
            "import sys; print('o' * 200000); print('e' * 200000, file=sys.stderr)",
        ],
    )
    result = await adapter.run(request(tmp_path, max_output_chars=100))
    assert len(result.stdout) == 100
    assert len(result.stderr) == 100
    assert result.stdout_truncated and result.stderr_truncated
    assert result.stdout_total_chars == 200_001
    assert result.stderr_total_chars == 200_001


@pytest.mark.asyncio
async def test_environment_is_filtered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOLIATH_SAFE_TEST", "visible")
    monkeypatch.setenv("MARKETPLACE_SECRET", "hidden")
    adapter = SubprocessAgentAdapter(
        name="environment",
        command_template=[
            sys.executable,
            "-c",
            (
                "import os; print(os.getenv('GOLIATH_SAFE_TEST')); "
                "print(os.getenv('MARKETPLACE_SECRET'))"
            ),
        ],
        environment_allowlist={"GOLIATH_SAFE_TEST"},
    )
    result = await adapter.run(request(tmp_path))
    assert result.stdout == "visible\nNone\n"
    assert "MARKETPLACE_SECRET" not in adapter._environment()


def test_no_shell_true_in_agent_implementation() -> None:
    agents_root = Path(__file__).parents[1] / "goliath" / "agents"
    source = "\n".join(path.read_text(encoding="utf-8") for path in agents_root.glob("*.py"))
    assert "shell=True" not in source


@pytest.mark.asyncio
async def test_codex_and_hermes_report_missing_without_crashing() -> None:
    codex = build_codex_adapter(
        AgentConfig(adapter="codex", command_template=["missing-codex-test"])
    )
    hermes = build_hermes_adapter(
        AgentConfig(adapter="hermes", command_template=["missing-hermes-test"])
    )
    assert not (await codex.healthcheck()).available
    assert not (await hermes.healthcheck()).available


def test_hermes_supports_executable_arguments_and_profile(tmp_path: Path) -> None:
    hermes = build_hermes_adapter(
        AgentConfig(
            adapter="hermes",
            command_template=["/opt/hermes", "--profile", "{profile}", "{workspace}"],
            profile="local",
        )
    )
    command, _ = hermes._command(request(tmp_path))
    assert command == ["/opt/hermes", "--profile", "local", str(tmp_path)]
