from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from goliath.agents.registry import AgentRegistry
from goliath.api import create_app
from goliath.config import OrchestrationConfig
from goliath.db.base import Base
from goliath.db.database import build_session_factory, create_test_engine
from goliath.db.models import AgentJobStatus, WorkerStatus
from goliath.db.operations import AuthRepository, LeaseRepository, WorkerRepository, hash_secret
from goliath.db.repositories import AgentJobRepository, InvalidStateTransitionError
from goliath.orchestration.scheduler import SchedulerService, next_run
from goliath.orchestration.service import JobOrchestrationService
from goliath.orchestration.uow import SqlAlchemyJobUnitOfWork
from tests.test_orchestration import FakeAdapter


def test_api_key_hashing_and_revocation(tmp_path: Path) -> None:
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        _, key, raw = AuthRepository(session).create_key("operator", ["jobs:read"])
        assert raw not in key.key_hash
        assert key.key_hash == hash_secret(raw)
        assert AuthRepository(session).authenticate(raw) is not None
        AuthRepository(session).revoke(key.id)
        assert AuthRepository(session).authenticate(raw) is None
    engine.dispose()


def test_lease_acquisition_heartbeat_and_stale_token(session) -> None:
    workers = WorkerRepository(session)
    workers.register("worker-a", "host", 123, 1, "test")
    jobs = AgentJobRepository(session)
    record = jobs.create(job_id=uuid4(), task_type="test", objective="lease", risk_tier="read_only")
    record = jobs.transition(
        record.id, AgentJobStatus.QUEUED, expected_version=record.version, agent_name="fake"
    )
    acquired = LeaseRepository(session).acquire("worker-a", 60)
    assert acquired is not None
    running, _lease, token = acquired
    assert running.status is AgentJobStatus.RUNNING
    LeaseRepository(session).heartbeat(running.id, "worker-a", token, 60)
    with pytest.raises(InvalidStateTransitionError):
        LeaseRepository(session).heartbeat(running.id, "worker-a", "stale", 60)


def test_expired_lease_is_reclaimed_by_worker(tmp_path: Path) -> None:
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        WorkerRepository(session).register("worker-a", "host", 123, 1, "test")
        jobs = AgentJobRepository(session)
        record = jobs.create(
            job_id=uuid4(), task_type="test", objective="recover", risk_tier="read_only"
        )
        record = jobs.transition(
            record.id, AgentJobStatus.QUEUED, expected_version=record.version, agent_name="fake"
        )
        acquired = LeaseRepository(session).acquire("worker-a", 60)
        assert acquired is not None
        _, lease, _ = acquired
        record_id = record.id
        lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    config = OrchestrationConfig(workspace_roots=[tmp_path], recovery_policy="requeue")
    daemon = __import__("goliath.orchestration.worker", fromlist=["WorkerDaemon"]).WorkerDaemon(
        session_factory=factory, registry=AgentRegistry(), config=config, worker_id="reaper"
    )
    assert daemon.reap() == 1
    with factory() as session:
        assert AgentJobRepository(session).get(record_id).status is AgentJobStatus.QUEUED
    engine.dispose()


def test_worker_status_stale_detection(session) -> None:
    repo = WorkerRepository(session)
    worker = repo.register("worker-a", "host", 1, 1, "test")
    worker.last_heartbeat_at = datetime.now(UTC) - timedelta(hours=1)
    stale = repo.mark_stale(datetime.now(UTC) - timedelta(minutes=1))
    assert stale == [worker]
    assert worker.status is WorkerStatus.STALE


def test_schedule_next_run_and_safe_task_validation(tmp_path: Path) -> None:
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    config = OrchestrationConfig(workspace_roots=[tmp_path])
    service = SchedulerService(
        uow_factory=lambda: SqlAlchemyJobUnitOfWork(factory),
        orchestration_service=None,
        config=config,
    )
    schedule = service.create(
        name="tests",
        task_type="test",
        objective_template="run tests",
        workspace=tmp_path,
        timing_type="interval",
        timing_value="60",
        owner="operator",
    )
    assert schedule.next_run_at is not None
    assert next_run(schedule, datetime.now(UTC)) is not None
    with pytest.raises(ValueError):
        service.create(
            name="unsafe",
            task_type="publish_listing",
            objective_template="bad",
            workspace=tmp_path,
            timing_type="once",
            timing_value=datetime.now(UTC).isoformat(),
            owner="operator",
        )
    engine.dispose()


def test_authenticated_api_and_idempotent_submission(tmp_path: Path) -> None:
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        _, _, raw_key = AuthRepository(session).create_key("operator", ["admin"])
        _, _, read_key = AuthRepository(session).create_key("reader", ["jobs:read"])
        session.commit()
    adapter = FakeAdapter()
    registry = AgentRegistry()
    registry.register("fake", adapter)
    config = OrchestrationConfig(workspace_roots=[tmp_path], api_authentication_required=True, metrics_enabled=True)
    service = JobOrchestrationService(
        uow_factory=lambda: SqlAlchemyJobUnitOfWork(factory), registry=registry, config=config
    )
    app = create_app(service=service, registry=registry, session_factory=factory, config=config)
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/agents").status_code == 401
    assert client.get("/agents", headers={"Authorization": f"Bearer {read_key}"}).status_code == 403
    assert client.get("/metrics", headers={"Authorization": f"Bearer {raw_key}"}).status_code == 200
    headers = {"Authorization": f"Bearer {raw_key}", "Idempotency-Key": "same-request"}
    payload = {
        "requested_agent": "fake",
        "task_type": "code_change",
        "objective": "API job",
        "workspace": str(tmp_path),
        "permissions": ["read_files", "write_files"],
    }
    first = client.post("/jobs", json=payload, headers=headers)
    second = client.post("/jobs", json=payload, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    conflict = client.post("/jobs", json={**payload, "objective": "different"}, headers=headers)
    assert conflict.status_code == 409
    engine.dispose()
