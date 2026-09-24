"""ManualGrader：人工评审占位（方案 4 / B-16，P1）。

规则：
- **不计入自动分**。产出的 ``human_review.jsonl`` 每行一个任务，``score`` 字段留给人工填写。
- 仍然执行确定性 checks，但把结果标注为 ``judge="manual"`` 且总分置 0，
  ``grader_mode="degraded"`` + ``degraded_reason="not_configured"``，
  以免自动流水线误把人工待评任务当成「已评过分」。
- 回灌：人工填好 ``score`` 后可用 :func:`load_human_review` 读取。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...utils.text import truncate
from ...utils.timex import now_iso
from ..grader import BaseGrader, GradeContext, run_checks
from ..models import AgentOutput, CheckResult, Task

__all__ = ["ManualGrader", "human_review_path", "write_human_review", "load_human_review"]

HUMAN_REVIEW_FILENAME = "human_review.jsonl"


def human_review_path(ctx: GradeContext, task: Task) -> Path:
    """人工评审文件的约定路径：``reports/<suite>/human_review.jsonl``。"""
    base = ctx.reports_dir or (ctx.task_dir.parent)
    return Path(base) / HUMAN_REVIEW_FILENAME


def write_human_review(ctx: GradeContext, task: Task, output: AgentOutput, checks: list[CheckResult]) -> None:
    """追加一行待评记录（**幂等**：同 run_id 覆盖而不是重复追加）。"""
    path = human_review_path(ctx, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "task_id": task.task_id,
        "run_id": output.run_id,
        "grader_type": "manual",
        "auto_checks": [c.to_dict() for c in checks],
        "answer_excerpt": truncate(output.final_answer, 2000)[0],
        "instructions": "请在下方 score 字段填写 0~100 的人工评分，完成后可用 load_human_review 回灌",
        "score": None,
        "reviewer": None,
        "reviewed_at": None,
        "created_at": now_iso(None),
    }
    existing: list[dict[str, Any]] = []
    if path.is_file():
        existing = load_human_review(path)
    replaced = False
    for index, item in enumerate(existing):
        if item.get("run_id") == record["run_id"] and item.get("task_id") == record["task_id"]:
            # 保留人工已填内容
            record["score"] = item.get("score")
            record["reviewer"] = item.get("reviewer")
            record["reviewed_at"] = item.get("reviewed_at")
            existing[index] = record
            replaced = True
            break
    if not replaced:
        existing.append(record)
    with path.open("w", encoding="utf-8") as fh:
        for item in existing:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")


def load_human_review(path: str | Path) -> list[dict[str, Any]]:
    """读取人工评审文件（跳过损坏行）。"""
    target = Path(path)
    if not target.is_file():
        return []
    out: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                out.append(item)
    return out


class ManualGrader(BaseGrader):
    name = "manual"
    version = "1.0"

    def grader_versions(self, ctx: GradeContext) -> dict[str, Any]:
        return {"deterministic": self.version, "manual": "1.0"}

    def evaluate(
        self, task: Task, output: AgentOutput, ctx: GradeContext
    ) -> tuple[list[CheckResult], str, str | None]:
        checks = run_checks(task.grader.checks, task, output, ctx)
        for check in checks:
            check.judge = "manual"
        if ctx.write_human_review:
            write_human_review(ctx, task, output, checks)
        return checks, "degraded", "not_configured"
