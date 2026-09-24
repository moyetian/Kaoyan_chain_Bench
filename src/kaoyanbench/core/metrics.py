"""统计聚合（方案 4.9 / 5.4 / B-18）。

- ``P90`` 用 ``statistics.quantiles(data, n=10, method="inclusive")[8]``；
  **样本 < 2 时 P90 = max**。
- ``null`` 值**不参与均值**（并记 ``*_n``），**不允许当 0**。
- 分组顺序固定：``by_category`` 按 8 类枚举序，``by_difficulty`` 按 4 难度枚举序。
- ``pass_at_k``：``Pass@1`` = 首次 attempt 成功率；``Pass@3`` = 3 次中至少 1 次成功的任务占比。
"""

from __future__ import annotations

import statistics
from typing import Any, Iterable, Mapping, Sequence

from .errors import ERROR_CODES, error_counts_ranked
from .models import (
    CATEGORIES,
    CATEGORY_LABELS,
    DIFFICULTIES,
    DIFFICULTY_LABELS,
    DIMENSIONS,
    Aggregates,
    GradeResult,
    Run,
)

__all__ = [
    "mean_or_none",
    "median_or_none",
    "p90_or_none",
    "sum_or_none",
    "pass_at_k",
    "aggregate",
    "group_aggregates",
    "score_breakdown_mean",
    "grader_mode_summary",
    "compare_direction",
]


# --------------------------------------------------------------------------- #
# 统计原语（null 一律排除）
# --------------------------------------------------------------------------- #
def _clean(values: Iterable[Any]) -> list[float]:
    out: list[float] = []
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out


def mean_or_none(values: Iterable[Any]) -> float | None:
    data = _clean(values)
    if not data:
        return None
    return round(sum(data) / len(data), 6)


def median_or_none(values: Iterable[Any]) -> float | None:
    data = _clean(values)
    if not data:
        return None
    return round(statistics.median(data), 6)


def p90_or_none(values: Iterable[Any]) -> float | None:
    """P90：``statistics.quantiles(n=10, method='inclusive')[8]``；样本 <2 → max。"""
    data = _clean(values)
    if not data:
        return None
    if len(data) < 2:
        return round(max(data), 6)
    ordered = sorted(data)
    try:
        return round(statistics.quantiles(ordered, n=10, method="inclusive")[8], 6)
    except (statistics.StatisticsError, IndexError):  # pragma: no cover - 极端
        return round(max(ordered), 6)


def sum_or_none(values: Iterable[Any]) -> float | None:
    data = _clean(values)
    if not data:
        return None
    return round(sum(data), 6)


def ratio_or_none(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def compare_direction(delta: float | None, *, higher_is_better: bool = True) -> str:
    """方向：``up`` / ``down`` / ``flat``（delta 为 0 或 None 时 flat）。"""
    if delta is None or abs(delta) < 1e-12:
        return "flat"
    if higher_is_better:
        return "up" if delta > 0 else "down"
    return "up" if delta < 0 else "down"


# --------------------------------------------------------------------------- #
# Pass@k
# --------------------------------------------------------------------------- #
def pass_at_k(results_by_task: Mapping[str, Sequence[bool]], k: int) -> float | None:
    """``Pass@1`` = 每个任务**首次** attempt 成功则计 1；

    ``Pass@k`` = 前 k 次中至少 1 次成功则计 1。分母是任务数。
    """
    if k < 1:
        return None
    total = 0
    passed = 0
    for task_id in sorted(results_by_task):
        results = [bool(x) for x in results_by_task[task_id]]
        if not results:
            continue
        total += 1
        window = results[:k]
        if any(window):
            passed += 1
    if total == 0:
        return None
    return round(passed / total, 6)


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #
def grader_mode_summary(grades: Sequence[GradeResult]) -> str:
    """全局 ``grader_mode``：全 full → ``full``；全 degraded → ``degraded``；否则 ``mixed``。"""
    if not grades:
        return "full"
    modes = {g.grader_mode for g in grades}
    if modes == {"full"}:
        return "full"
    if modes == {"degraded"}:
        return "degraded"
    if "degraded" in modes:
        return "mixed"
    return "full"


def _latest_grades_by_task(grades: Sequence[GradeResult]) -> dict[str, GradeResult]:
    """每个任务取 ``run_id`` 最大的那次作为代表（确定性：按 run_id 排序）。"""
    out: dict[str, GradeResult] = {}
    for grade in sorted(grades, key=lambda g: (g.task_id, g.run_id)):
        out[grade.task_id] = grade
    return out


def _runs_by_task(runs: Sequence[Run]) -> dict[str, list[Run]]:
    out: dict[str, list[Run]] = {}
    for run in runs:
        out.setdefault(run.task_id, []).append(run)
    for key in out:
        out[key].sort(key=lambda r: (r.attempt, r.run_id))
    return out


def _success_by_task(
    grades: Sequence[GradeResult], runs: Sequence[Run]
) -> dict[str, list[bool]]:
    """每个任务各次 attempt 的成功与否（用于 Pass@k）。"""
    grade_by_run = {g.run_id: g for g in grades}
    result: dict[str, list[bool]] = {}
    for task_id in sorted({r.task_id for r in runs} | {g.task_id for g in grades}):
        task_runs = sorted(
            (r for r in runs if r.task_id == task_id), key=lambda r: (r.attempt, r.run_id)
        )
        flags: list[bool] = []
        for run in task_runs:
            grade = grade_by_run.get(run.run_id)
            flags.append(bool(grade.metrics.task_success) if grade else bool(run.success))
        if not flags:
            task_grades = sorted(
                (g for g in grades if g.task_id == task_id), key=lambda g: g.run_id
            )
            flags = [bool(g.metrics.task_success) for g in task_grades]
        result[task_id] = flags
    return result


def aggregate(
    grades: Sequence[GradeResult],
    runs: Sequence[Run],
    *,
    task_meta: Mapping[str, Mapping[str, Any]] | None = None,
    include_groups: bool = True,
) -> Aggregates:
    """计算方案 4.9 ``aggregates`` 的全部字段。

    ``task_meta``：``{task_id: {"category": ..., "difficulty": ...}}``，用于分组聚合；
    不传时会尝试从 ``runs`` 推断（``Run`` 不含 category，故推荐显式传入）。
    """
    latest = _latest_grades_by_task(grades)
    success_map = _success_by_task(grades, runs)
    runs_map = _runs_by_task(runs)

    n_tasks = len(latest)
    success_flags = [bool(g.metrics.task_success) for g in latest.values()]
    n_success = sum(1 for f in success_flags if f)

    agg = Aggregates(n_tasks=n_tasks)
    agg.task_success_rate = ratio_or_none(n_success, n_tasks)
    agg.failure_rate = ratio_or_none(n_tasks - n_success, n_tasks)
    agg.pass_at_1 = pass_at_k(success_map, 1)
    agg.pass_at_3 = pass_at_k(success_map, 3)

    scores = [g.score_total for g in latest.values()]
    agg.score_mean = mean_or_none(scores)
    agg.score_median = median_or_none(scores)
    agg.score_p90 = p90_or_none(scores)

    agg.factuality_mean = mean_or_none(g.metrics.factuality for g in latest.values())
    agg.source_precision_mean = mean_or_none(g.metrics.source_precision for g in latest.values())
    agg.search_recall_mean = mean_or_none(g.metrics.search_recall for g in latest.values())
    agg.citation_accuracy_mean = mean_or_none(
        g.metrics.citation_accuracy for g in latest.values()
    )
    agg.hallucination_rate_mean = mean_or_none(
        g.metrics.hallucination_rate for g in latest.values()
    )
    agg.tool_success_rate_mean = mean_or_none(
        g.metrics.tool_success_rate for g in latest.values()
    )

    tool_calls_all = [g.metrics.tool_calls for g in latest.values()]
    agg.tool_calls_mean = mean_or_none(tool_calls_all)
    agg.tool_calls_p90 = p90_or_none(tool_calls_all)

    agg.tokens_mean = mean_or_none(g.metrics.tokens for g in latest.values())
    agg.tokens_p90 = p90_or_none(g.metrics.tokens for g in latest.values())

    latencies = [g.metrics.latency_seconds for g in latest.values()]
    agg.latency_seconds_mean = mean_or_none(latencies)
    agg.latency_seconds_median = median_or_none(latencies)
    agg.latency_seconds_p90 = p90_or_none(latencies)

    agg.cost_usd_mean = mean_or_none(g.metrics.cost_usd for g in latest.values())
    agg.cost_usd_total = sum_or_none(g.metrics.cost_usd for g in latest.values())

    # usage 质量指标（基于全部 runs，不是 latest grade）
    missing = 0
    estimated = 0
    with_usage = 0
    for run in runs:
        usage = run.usage
        if usage is None or usage.usage_source == "none":
            missing += 1
            continue
        with_usage += 1
        if usage.estimated:
            estimated += 1
    agg.usage_missing_count = missing
    if with_usage > 0:
        agg.usage_estimated_ratio = round(estimated / with_usage, 6)
    else:
        agg.usage_estimated_ratio = None

    agg.degraded_task_count = sum(1 for g in latest.values() if g.grader_mode == "degraded")
    agg.grader_mode = grader_mode_summary(list(latest.values()))

    all_errors: list[Any] = []
    for grade in latest.values():
        all_errors.extend(grade.errors or [])
    agg.error_counts = error_counts_ranked(all_errors)

    # 样本数（说明 null 不参与均值）
    agg.sample_counts = {
        "score": len(_clean(scores)),
        "factuality": len(_clean(g.metrics.factuality for g in latest.values())),
        "source_precision": len(_clean(g.metrics.source_precision for g in latest.values())),
        "search_recall": len(_clean(g.metrics.search_recall for g in latest.values())),
        "citation_accuracy": len(_clean(g.metrics.citation_accuracy for g in latest.values())),
        "tool_success_rate": len(_clean(g.metrics.tool_success_rate for g in latest.values())),
        "tokens": len(_clean(g.metrics.tokens for g in latest.values())),
        "cost_usd": len(_clean(g.metrics.cost_usd for g in latest.values())),
    }

    if include_groups:
        meta = dict(task_meta or _infer_task_meta(grades, runs))
        agg.by_category = group_aggregates(latest, runs_map, meta, group_key="category")
        agg.by_difficulty = group_aggregates(latest, runs_map, meta, group_key="difficulty")
    return agg


def _infer_task_meta(
    grades: Sequence[GradeResult], runs: Sequence[Run]
) -> dict[str, dict[str, Any]]:
    """无外部 meta 时的降级推断：只填 category='unknown'（**不猜测**类别）。"""
    ids = sorted({g.task_id for g in grades} | {r.task_id for r in runs})
    return {task_id: {"category": "unknown", "difficulty": "unknown"} for task_id in ids}


def group_aggregates(
    latest: Mapping[str, GradeResult],
    runs_map: Mapping[str, Sequence[Run]],
    task_meta: Mapping[str, Mapping[str, Any]],
    *,
    group_key: str,
) -> list[dict[str, Any]]:
    """按 ``category`` / ``difficulty`` 分组聚合。

    输出顺序**固定**：先按枚举序（8 类 / 4 难度），再追加未知分组（按名称升序）。
    """
    buckets: dict[str, list[str]] = {}
    for task_id in sorted(latest):
        meta = task_meta.get(task_id) or {}
        key = str(meta.get(group_key) or "unknown")
        buckets.setdefault(key, []).append(task_id)

    enum_order = CATEGORIES if group_key == "category" else DIFFICULTIES
    labels = CATEGORY_LABELS if group_key == "category" else DIFFICULTY_LABELS
    ordered = [k for k in enum_order if k in buckets]
    ordered += sorted(k for k in buckets if k not in enum_order)

    out: list[dict[str, Any]] = []
    for key in ordered:
        ids = buckets[key]
        group_grades = [latest[tid] for tid in ids]
        success = [bool(g.metrics.task_success) for g in group_grades]
        success_map = {tid: _success_flags(latest[tid], runs_map.get(tid, ())) for tid in ids}
        entry: dict[str, Any] = {
            group_key: key,
            "label": labels.get(key, key),
            "task_count": len(group_grades),
            "task_success_rate": ratio_or_none(sum(1 for s in success if s), len(success)),
            "score_mean": mean_or_none(g.score_total for g in group_grades),
            "factuality_mean": mean_or_none(g.metrics.factuality for g in group_grades),
            "search_recall_mean": mean_or_none(g.metrics.search_recall for g in group_grades),
            "source_precision_mean": mean_or_none(
                g.metrics.source_precision for g in group_grades
            ),
            "citation_accuracy_mean": mean_or_none(
                g.metrics.citation_accuracy for g in group_grades
            ),
            "hallucination_rate_mean": mean_or_none(
                g.metrics.hallucination_rate for g in group_grades
            ),
            "latency_seconds_mean": mean_or_none(
                g.metrics.latency_seconds for g in group_grades
            ),
            "tool_calls_mean": mean_or_none(g.metrics.tool_calls for g in group_grades),
            "cost_usd_mean": mean_or_none(g.metrics.cost_usd for g in group_grades),
            "pass_at_1": pass_at_k(success_map, 1),
            "pass_at_3": pass_at_k(success_map, 3),
        }
        out.append(entry)
    return out


def _success_flags(grade: GradeResult, runs: Sequence[Run]) -> list[bool]:
    if runs:
        return [bool(grade.metrics.task_success)] * len(runs)
    return [bool(grade.metrics.task_success)]


def score_breakdown_mean(grades: Sequence[GradeResult]) -> dict[str, float | None]:
    """7 维均值 + total（方案 4.9 ``score_breakdown_mean``）。"""
    latest = _latest_grades_by_task(grades)
    out: dict[str, float | None] = {}
    for dim in DIMENSIONS:
        out[dim] = mean_or_none(getattr(g.score, dim, None) for g in latest.values())
    out["total"] = mean_or_none(g.score_total for g in latest.values())
    return out


def error_sample_tasks(grades: Sequence[GradeResult], code: str, limit: int = 3) -> list[str]:
    """某错误码的样例任务 id（按 task_id 升序，最多 ``limit`` 个）。"""
    out: list[str] = []
    for task_id in sorted({g.task_id for g in grades}):
        for grade in sorted(
            (g for g in grades if g.task_id == task_id), key=lambda g: g.run_id
        ):
            if any(e.code == code for e in (grade.errors or [])):
                out.append(task_id)
                break
        if len(out) >= limit:
            break
    return out


def empty_error_breakdown() -> list[dict[str, Any]]:
    """11 类错误的**零值**列表（count=0 也要列出，方案 5.7）。"""
    return [{"code": code, "count": 0, "ratio": 0.0, "sample_task_ids": []} for code in ERROR_CODES]
