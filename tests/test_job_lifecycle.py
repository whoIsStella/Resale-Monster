from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from goliath.db.models import AgentJobStatus
from goliath.db.repositories import AgentJobRepository, InvalidStateTransitionError


def create_job(repository: AgentJobRepository):
    return repository.create(
        job_id=uuid4(),
        task_type="test",
        objective="Exercise lifecycle",
        risk_tier="read_only",
        workspace_path="/tmp",
    )


def reach(repository: AgentJobRepository, status: AgentJobStatus):
    record = create_job(repository)
    if status is AgentJobStatus.PENDING:
        return record
    record = repository.transition(
        record.id,
        AgentJobStatus.QUEUED,
        expected_version=record.version,
        agent_name="fake",
    )
    if status is AgentJobStatus.QUEUED:
        return record
    return repository.transition(
        record.id,
        AgentJobStatus.RUNNING,
        expected_version=record.version,
    )


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (AgentJobStatus.PENDING, AgentJobStatus.QUEUED),
        (AgentJobStatus.PENDING, AgentJobStatus.CANCELLED),
        (AgentJobStatus.PENDING, AgentJobStatus.FAILED),
        (AgentJobStatus.QUEUED, AgentJobStatus.RUNNING),
        (AgentJobStatus.QUEUED, AgentJobStatus.CANCELLED),
        (AgentJobStatus.QUEUED, AgentJobStatus.FAILED),
        (AgentJobStatus.RUNNING, AgentJobStatus.SUCCEEDED),
        (AgentJobStatus.RUNNING, AgentJobStatus.FAILED),
        (AgentJobStatus.RUNNING, AgentJobStatus.TIMED_OUT),
        (AgentJobStatus.RUNNING, AgentJobStatus.CANCELLED),
    ],
)
def test_every_valid_transition(
    session: Session, source: AgentJobStatus, target: AgentJobStatus
) -> None:
    repository = AgentJobRepository(session)
    record = reach(repository, source)
    transitioned = repository.transition(
        record.id,
        target,
        expected_version=record.version,
        agent_name="fake" if target is AgentJobStatus.QUEUED else None,
        cancellation_reason="operator" if target is AgentJobStatus.CANCELLED else None,
    )
    assert transitioned.status is target
    assert transitioned.version == record.version
    if target is AgentJobStatus.QUEUED:
        assert transitioned.queued_at is not None
    if target is AgentJobStatus.RUNNING:
        assert transitioned.started_at is not None
    if target is AgentJobStatus.CANCELLED:
        assert transitioned.cancelled_at is not None
        assert transitioned.completed_at is not None
    if target in {
        AgentJobStatus.SUCCEEDED,
        AgentJobStatus.FAILED,
        AgentJobStatus.TIMED_OUT,
    }:
        assert transitioned.completed_at is not None


@pytest.mark.parametrize(
    ("terminal", "target"),
    [
        (AgentJobStatus.SUCCEEDED, AgentJobStatus.RUNNING),
        (AgentJobStatus.FAILED, AgentJobStatus.QUEUED),
        (AgentJobStatus.CANCELLED, AgentJobStatus.SUCCEEDED),
        (AgentJobStatus.TIMED_OUT, AgentJobStatus.RUNNING),
    ],
)
def test_terminal_states_reject_transitions(
    session: Session, terminal: AgentJobStatus, target: AgentJobStatus
) -> None:
    repository = AgentJobRepository(session)
    source = (
        AgentJobStatus.QUEUED if terminal is AgentJobStatus.CANCELLED else AgentJobStatus.RUNNING
    )
    record = reach(repository, source)
    record = repository.transition(
        record.id,
        terminal,
        expected_version=record.version,
        cancellation_reason="operator" if terminal is AgentJobStatus.CANCELLED else None,
    )
    with pytest.raises(InvalidStateTransitionError, match="invalid agent job transition"):
        repository.transition(record.id, target, expected_version=record.version)


def test_stale_version_rejects_duplicate_claim(session: Session) -> None:
    repository = AgentJobRepository(session)
    record = reach(repository, AgentJobStatus.QUEUED)
    stale_version = record.version
    repository.transition(
        record.id,
        AgentJobStatus.RUNNING,
        expected_version=stale_version,
    )
    with pytest.raises(InvalidStateTransitionError):
        repository.transition(
            record.id,
            AgentJobStatus.CANCELLED,
            expected_version=stale_version,
        )
