from pathlib import Path

from goliath.core.schemas import AgentJob


def test_job_accepts_existing_workspace(tmp_path: Path) -> None:
    job = AgentJob(task_type="test", objective="Run tests", workspace=tmp_path)
    assert job.workspace == tmp_path.resolve()
