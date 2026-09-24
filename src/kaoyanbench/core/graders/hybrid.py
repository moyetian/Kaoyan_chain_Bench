"""HybridGrader：确定性为主 + 语义兜底（方案 5.3 / B-16）。

合并规则（写死，可单测）：
- 以 ``DeterministicGrader`` 的 checks 为主干（``judge="deterministic"``）。
- 若配置了 rubric 且 semantic client 可用 → 追加 ``judge="semantic"`` 的 checks。
- 若 semantic 不可用 → **仅**保留确定性 checks，``grader_mode="degraded"``，
  ``degraded_reason="no_api_key"``；其余规则化补充的 checks 由 ``SemanticGrader`` 提供
  （见 :func:`_degraded_rubric_checks`），保证「不允许静默假装评过」。
- 任一 semantic 判失败 → ``grader_mode="degraded"`` 并写明原因。
"""

from __future__ import annotations

from typing import Any

from ..grader import BaseGrader, GradeContext, run_checks
from ..models import AgentOutput, CheckResult, Task
from .semantic import (
    _context_text,
    _degraded_checks,
    _normalize_reason,
    fallback_semantic_score,
)

__all__ = ["HybridGrader"]


class HybridGrader(BaseGrader):
    name = "hybrid"
    version = "1.0"

    def grader_versions(self, ctx: GradeContext) -> dict[str, Any]:
        client = ctx.semantic_client
        return {
            "deterministic": self.version,
            "semantic_prompt": (
                task_prompt_version(self.spec) if self.spec.rubric else None
            ) or getattr(client, "prompt_version", None),
            "judge_model": getattr(client, "model", None),
        }

    def evaluate(
        self, task: Task, output: AgentOutput, ctx: GradeContext
    ) -> tuple[list[CheckResult], str, str | None]:
        checks = run_checks(task.grader.checks, task, output, ctx)
        criteria = list(task.grader.rubric.criteria) if task.grader.rubric else []
        if not criteria:
            # 没有 rubric → 纯确定性行为，但类型仍如实上报为 hybrid
            return checks, "full", None

        client = ctx.semantic_client
        if client is None or not client.available():
            return (
                checks + _degraded_checks(task, output, criteria),
                "degraded",
                "no_api_key",
            )

        semantic_checks: list[CheckResult] = []
        failed_reason: str | None = None
        for criterion in criteria:
            reply = client.judge(criterion.question, output.final_answer, _context_text(task, ctx))
            if reply.error:
                failed_reason = failed_reason or reply.error
                score, detail = fallback_semantic_score(task, output, criterion)
                semantic_checks.append(
                    CheckResult(
                        id=criterion.id,
                        type="semantic_rubric",
                        dimension=criterion.dimension,
                        passed=score >= 0.5,
                        weight=float(criterion.weight),
                        critical=False,
                        detail=f"降级（{reply.error}）：{detail}",
                        judge="semantic",
                    )
                )
                continue
            passed = reply.supported if reply.supported is not None else (
                (reply.score or 0.0) >= 0.5
            )
            semantic_checks.append(
                CheckResult(
                    id=criterion.id,
                    type="semantic_rubric",
                    dimension=criterion.dimension,
                    passed=passed,
                    weight=float(criterion.weight),
                    critical=False,
                    detail=f"语义判定：{reply.reason or ('满足标准' if passed else '不满足标准')}",
                    judge="semantic",
                )
            )

        if failed_reason:
            return checks + semantic_checks, "degraded", _normalize_reason(failed_reason)
        return checks + semantic_checks, "full", None


def task_prompt_version(spec: Any) -> str | None:
    rubric = getattr(spec, "rubric", None)
    return getattr(rubric, "prompt_version", None) if rubric else None
