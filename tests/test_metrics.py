"""B-18：11 项指标 + Mean/Median/P90 + 分组 + Pass@1/Pass@3。"""

from __future__ import annotations

import pytest

from kaoyanbench.core.metrics import (
    aggregate,
    grader_mode_summary,
    mean_or_none,
    median_or_none,
    p90_or_none,
    pass_at_k,
    ratio_or_none,
    score_breakdown_mean,
)
from kaoyanbench.core.models import (
    Aggregates,
    Environment,
    GradeResult,
    Metrics,
    Run,
    ScoreBreakdown,
)


def test_mean_ignores_none() -> None:
    assert mean_or_none([1.0, None, 3.0]) == 2.0


def test_mean_all_none_is_none() -> None:
    assert mean_or_none([None, None]) is None


def test_median_or_none() -> None:
    assert median_or_none([1.0, 2.0, 3.0]) == 2.0


def test_p90_known_samples() -> None:
    # 10 个已知样本 1..10，P90（statistics.quantiles inclusive，线性插值）→ 9.1
    values = [float(i) for i in range(1, 11)]
    assert p90_or_none(values) == pytest.approx(9.1, abs=1e-9)


def test_p90_single_sample_returns_max() -> None:
    assert p90_or_none([5.0]) == 5.0


def test_ratio_or_none() -> None:
    assert ratio_or_none(1, 4) == 0.25
    assert ratio_or_none(0, 0) is None


def test_pass_at_k() -> None:
    results = {"A": [True, True, False], "B": [False, False, False]}
    # A: 至少一次成功，B: 无
    assert pass_at_k(results, 1) == 0.5


def test_grader_mode_summary() -> None:
    g1 = GradeResult(task_id="A", run_id="r1", grader_mode="full")
    g2 = GradeResult(task_id="B", run_id="r2", grader_mode="degraded")
    assert grader_mode_summary([g1, g2]) == "mixed"
    assert grader_mode_summary([g1, g1]) == "full"
    assert grader_mode_summary([g2, g2]) == "degraded"
    assert grader_mode_summary([]) == "full"


def _mk_grade(task_id: str, score: float, success: bool, mode: str = "full") -> GradeResult:
    return GradeResult(
        task_id=task_id,
        run_id=f"run_{task_id}",
        grader_mode=mode,
        score=ScoreBreakdown(total=score, factuality=score * 0.3),
        metrics=Metrics(task_success=success, factuality=score / 100.0, latency_seconds=score),
    )


def _mk_run(task_id: str, latency_ms: int = 1000) -> Run:
    return Run(
        run_id=f"run_{task_id}",
        task_id=task_id,
        agent="mock",
        model="m",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
        duration_ms=latency_ms,
        environment=Environment(),
    )


def test_aggregate_basic() -> None:
    grades = [_mk_grade("A", 80.0, True), _mk_grade("B", 40.0, False)]
    runs = [_mk_run("A"), _mk_run("B")]
    agg = aggregate(grades, runs)
    assert agg.n_tasks == 2
    assert agg.task_success_rate == 0.5
    assert agg.failure_rate == 0.5
    assert agg.score_mean == 60.0


def test_aggregate_none_metrics_not_in_mean() -> None:
    g1 = _mk_grade("A", 80.0, True)
    g2 = _mk_grade("B", 40.0, False)
    g2.metrics.factuality = None
    agg = aggregate([g1, g2], [_mk_run("A"), _mk_run("B")])
    # factuality 仅 A 参与
    assert agg.factuality_mean == pytest.approx(0.8)


def test_score_breakdown_mean() -> None:
    mean = score_breakdown_mean([_mk_grade("A", 80.0, True), _mk_grade("B", 60.0, True)])
    assert mean["total"] == 70.0


def test_grader_mode_degraded_count() -> None:
    grades = [_mk_grade("A", 80.0, True, mode="degraded"), _mk_grade("B", 60.0, True)]
    agg = aggregate(grades, [_mk_run("A"), _mk_run("B")])
    assert agg.degraded_task_count == 1
    assert agg.grader_mode == "mixed"


def test_aggregate_empty() -> None:
    agg = aggregate([], [])
    assert agg.n_tasks == 0
    assert agg.task_success_rate is None
    assert agg.grader_mode == "full"
