from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from goliath import cli
from goliath.agents.registry import AgentRegistry
from goliath.config import OrchestrationConfig
from goliath.db.base import Base
from goliath.db.database import build_session_factory, create_test_engine
from goliath.orchestration.service import JobOrchestrationService
from goliath.orchestration.uow import SqlAlchemyJobUnitOfWork
from tests.test_orchestration import FakeAdapter

runner = CliRunner()


def runtime(tmp_path: Path, *, available: bool = True):
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    registry = AgentRegistry()
    registry.register("fake", FakeAdapter(available=available))
    config = OrchestrationConfig(
        workspace_roots=[tmp_path],
        default_timeout_seconds=10,
        maximum_timeout_seconds=20,
        default_output_limit_chars=1_000,
        maximum_output_limit_chars=2_000,
    )
    service = JobOrchestrationService(
        uow_factory=lambda: SqlAlchemyJobUnitOfWork(factory),
        registry=registry,
        config=config,
    )
    return cli.Runtime(config=config, registry=registry, service=service), engine


def test_cli_submit_and_list_json(tmp_path: Path, monkeypatch) -> None:
    test_runtime, engine = runtime(tmp_path)
    monkeypatch.setattr(cli, "build_runtime", lambda: test_runtime)
    monkeypatch.setattr(
        cli, "build_agent_registry", lambda: (test_runtime.config, test_runtime.registry)
    )
    submitted = runner.invoke(
        cli.app,
        [
            "job",
            "submit",
            "--agent",
            "fake",
            "--workspace",
            str(tmp_path),
            "--objective",
            "Test CLI",
            "--permission",
            "read_files",
            "--permission",
            "write_files",
            "--capability",
            "coding",
            "--json",
        ],
    )
    assert submitted.exit_code == 0, submitted.output
    assert '"status": "queued"' in submitted.output

    listed = runner.invoke(cli.app, ["job", "list", "--json"])
    assert listed.exit_code == 0
    assert '"objective": "Test CLI"' in listed.output
    engine.dispose()


def test_cli_expected_failure_has_no_traceback(tmp_path: Path, monkeypatch) -> None:
    test_runtime, engine = runtime(tmp_path)
    monkeypatch.setattr(cli, "build_runtime", lambda: test_runtime)
    monkeypatch.setattr(
        cli, "build_agent_registry", lambda: (test_runtime.config, test_runtime.registry)
    )
    result = runner.invoke(cli.app, ["job", "status", str(uuid4())])
    assert result.exit_code == 1
    assert "error: agent job not found" in result.output
    assert "Traceback" not in result.output
    engine.dispose()


def test_cli_doctor_reports_unavailable_agent(tmp_path: Path, monkeypatch) -> None:
    test_runtime, engine = runtime(tmp_path, available=False)
    monkeypatch.setattr(cli, "build_runtime", lambda: test_runtime)
    monkeypatch.setattr(
        cli, "build_agent_registry", lambda: (test_runtime.config, test_runtime.registry)
    )
    result = runner.invoke(cli.app, ["agent", "doctor", "--json"])
    assert result.exit_code == 1
    assert '"available": false' in result.output
    engine.dispose()


def test_cli_submit_returns_nonzero_when_no_agent_is_eligible(tmp_path: Path, monkeypatch) -> None:
    test_runtime, engine = runtime(tmp_path, available=False)
    monkeypatch.setattr(cli, "build_runtime", lambda: test_runtime)
    result = runner.invoke(
        cli.app,
        [
            "job",
            "submit",
            "--agent",
            "fake",
            "--workspace",
            str(tmp_path),
            "--objective",
            "Cannot run",
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert '"status": "failed"' in result.output
    assert "Traceback" not in result.output
    engine.dispose()
