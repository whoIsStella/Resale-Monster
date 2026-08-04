from pathlib import Path

import pytest
from pydantic import ValidationError

from goliath.config import AgentConfig, OrchestrationConfig
from goliath.core.schemas import REQUIRED_FORBIDDEN_ACTIONS, JobSubmission


def test_submission_resolves_context_inside_workspace(tmp_path: Path) -> None:
    context = tmp_path / "context.txt"
    context.write_text("safe", encoding="utf-8")
    submission = JobSubmission(
        task_type="review",
        objective="Review context",
        workspace=tmp_path,
        context_files=[Path("context.txt")],
        forbidden_actions=set(REQUIRED_FORBIDDEN_ACTIONS),
    )
    assert submission.workspace == tmp_path.resolve()
    assert submission.context_files == [context.resolve()]


def test_submission_rejects_path_traversal(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(ValidationError, match="outside workspace"):
        JobSubmission(
            task_type="review",
            objective="Escape",
            workspace=tmp_path,
            context_files=[Path("../outside.txt")],
            forbidden_actions=set(REQUIRED_FORBIDDEN_ACTIONS),
        )


def test_submission_rejects_unsupported_values_and_missing_boundaries(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        JobSubmission(
            task_type="deploy",
            objective="Unsupported",
            workspace=tmp_path,
            permissions={"root"},
            forbidden_actions={"publish_listing"},
        )


def test_configuration_rejects_secrets_and_outside_workspace(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="unsafe environment"):
        AgentConfig(
            command_template=["agent"],
            environment_allowlist={"PATH", "OPENAI_API_KEY"},
        )
    config = OrchestrationConfig(workspace_roots=[tmp_path])
    with pytest.raises(ValueError, match="outside configured roots"):
        config.validate_workspace(tmp_path.parent)


def test_configuration_enforces_default_maximum_relationship(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="default timeout exceeds"):
        OrchestrationConfig(
            workspace_roots=[tmp_path],
            default_timeout_seconds=10,
            maximum_timeout_seconds=5,
        )
