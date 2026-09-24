"""DeterministicGrader：纯声明式 checks 评分（方案 B-13）。

**同输入同输出**：不读时间、不用随机、不遍历无序容器。跑两次结果必须完全一致。
所有维度都由 checks 驱动，另有若干由 Metrics 驱动的隐式检查（tool_execution / efficiency
在 scorer 中处理，见 ``scorer.score_from_checks``）。
"""

from __future__ import annotations

from typing import Any

from ..grader import BaseGrader, GradeContext, run_checks
from ..models import AgentOutput, CheckResult, Task

__all__ = ["DeterministicGrader"]


class DeterministicGrader(BaseGrader):
    name = "deterministic"
    version = "1.0"

    def evaluate(
        self, task: Task, output: AgentOutput, ctx: GradeContext
    ) -> tuple[list[CheckResult], str, str | None]:
        checks = run_checks(task.grader.checks, task, output, ctx)
        # 若任务未声明任何 check 但 ground_truth 存在 → 生成「字段级等值」隐式检查，
        # 避免完全无判据（但仍显式标注 judge=deterministic，不伪装成人工判定）。
        if not checks and task.expected.ground_truth:
            checks = _implicit_ground_truth_checks(task, output, ctx)
        return checks, "full", None


def _implicit_ground_truth_checks(
    task: Task, output: AgentOutput, ctx: GradeContext
) -> list[CheckResult]:
    """用 ``ground_truth`` 生成隐式数值/等值检查（仅 offline + fixture 任务会走这里）。"""
    from ..utils.text import approx_equal, get_by_path, safe_str
    from ..checks import answer_payload

    payload = answer_payload(output)
    results: list[CheckResult] = []
    ground_truth = task.expected.ground_truth or {}
    for index, key in enumerate(sorted(ground_truth)):
        expected = ground_truth[key]
        actual = get_by_path(payload, key)
        if actual is None and isinstance(payload, dict):
            actual = payload.get(key)
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            passed = actual is not None and approx_equal(actual, expected, 1e-6)
            detail = f"ground_truth.{key} 期望 {expected!r}，实际 {safe_str(actual)[:40]!r}"
        else:
            passed = safe_str(actual) == safe_str(expected)
            detail = f"ground_truth.{key} 期望 {safe_str(expected)[:40]!r}"
        results.append(
            CheckResult(
                id=f"gt-{index + 1}",
                type="numeric" if isinstance(expected, (int, float)) else "string_eq",
                dimension="factuality",
                passed=passed,
                weight=1.0,
                critical=False,
                detail=detail,
                judge="deterministic",
            )
        )
    return results


def _unused(_: Any) -> None:  # pragma: no cover
    return None
