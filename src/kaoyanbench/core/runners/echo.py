"""EchoRunner：回显型 Runner（方案 B-06）。

用途：pipeline 烟囱测试。把指令原样回显为 ``final_answer``，
**不伪造任何** tool_calls / sources / usage。
"""

from __future__ import annotations

from ...utils.timex import utcnow_iso
from ..models import AgentOutput, RunContext, Task, Usage
from ..runner import BaseRunner, runner_environment

__all__ = ["EchoRunner"]


class EchoRunner(BaseRunner):
    type_key = "echo"

    def execute(self, task: Task, ctx: RunContext) -> AgentOutput:
        started = utcnow_iso()
        answer = (
            f"[echo] {task.task_id}\n"
            f"{task.instruction}\n"
            f"(network={task.network}, time_limit={task.time_limit}s, tool_limit={task.tool_limit})"
        )
        return AgentOutput(
            run_id=ctx.run_id,
            agent=self.name,
            agent_version=self.spec.version or "1.0",
            model=self.spec.model or f"echo/{self.name}",
            provider="local",
            task_id=task.task_id,
            started_at=started,
            ended_at=utcnow_iso(),
            duration_ms=0,
            final_answer=answer,
            answer_files={},
            tool_calls=[],
            sources=[],
            citations=[],
            errors=[],
            usage=Usage.none(),
            timed_out=False,
            exit_code=0,
            stdout=answer,
            stderr="",
            raw={"runner": "echo"},
            environment=runner_environment(),
        )
