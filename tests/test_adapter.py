from pathlib import Path

from goliath.agents.subprocess_adapter import SubprocessAgentAdapter
from goliath.core.schemas import AgentJob


def test_prompt_contains_boundaries(tmp_path: Path) -> None:
    adapter = SubprocessAgentAdapter(name="fake", command_template=["echo", "{prompt}"])
    job = AgentJob(
        task_type="code_change",
        objective="Create an inventory model",
        workspace=tmp_path,
        forbidden_actions=["publish_listing"],
    )
    prompt = adapter._prompt(job)
    assert "Create an inventory model" in prompt
    assert "publish_listing" in prompt
