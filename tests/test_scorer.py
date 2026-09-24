"""B-17：100 分制打分 + 维度中性重分配 + Efficiency 规则。"""

from __future__ import annotations

import pytest

from kaoyanbench.core.errors import ScoringError
from kaoyanbench.core.models import DEFAULT_WEIGHTS, DIMENSIONS, CheckResult
from kaoyanbench.core.scorer import (
    compute_efficiency,
    dimension_ratios,
    effective_weights,
    hard_gate_failed,
    score_from_checks,
    task_success,
)


def _c(cid: str, dim: str, passed: bool | None, critical: bool = False, weight: float = 1.0) -> CheckResult:
    return CheckResult(id=cid, type="string_contains", dimension=dim, passed=passed, critical=critical, weight=weight)


def test_all_neutral_raises_scoring_error() -> None:
    checks = [_c("c1", "factuality", None), _c("c2", "citation", None)]
    with pytest.raises(ScoringError):
        score_from_checks(checks, DEFAULT_WEIGHTS)


def test_empty_checks_raises() -> None:
    with pytest.raises(ScoringError):
        score_from_checks([], DEFAULT_WEIGHTS)


def test_perfect_all_pass_is_100() -> None:
    checks = [
        _c("c1", "factuality", True),
        _c("c2", "source_quality", True),
        _c("c3", "citation", True),
        _c("c4", "completeness", True),
        _c("c5", "task_completion", True),
    ]
    sb = score_from_checks(checks, DEFAULT_WEIGHTS)
    assert abs(sb.total - 100.0) < 1e-3  # 4 位小数四舍五入累积误差
    # tool_execution / efficiency 无 check + 无运行指标 → neutral → 触发重分配
    assert sb.reallocated is True
    assert "tool_execution" in sb.neutral_dimensions


def test_all_seven_dimensions_covered_no_reallocation() -> None:
    checks = [_c(f"c{i}", d, True) for i, d in enumerate(DIMENSIONS)]
    sb = score_from_checks(
        checks,
        DEFAULT_WEIGHTS,
        tool_success_rate=1.0,
        tool_calls=0,
        tool_limit=10,
        latency_seconds=0,
        time_limit=60,
    )
    assert abs(sb.total - 100.0) < 1e-6
    assert sb.reallocated is False
    assert sb.tool_execution == 10.0
    assert sb.efficiency == 5.0


def test_half_pass_is_reported_correctly() -> None:
    checks = [
        _c("c1", "factuality", True),
        _c("c2", "factuality", False),
    ]
    sb = score_from_checks(checks, DEFAULT_WEIGHTS)
    # 单一维度 neutral 重分配后，factuality 占全部有效权重，通过一半 → 50
    assert abs(sb.factuality - 50.0) < 1e-6


def test_neutral_dimension_reallocation_sum_100() -> None:
    # 只有 factuality 有检查项且全通过 → 应重分配，总分 100
    checks = [_c("c1", "factuality", True)]
    sb = score_from_checks(checks, DEFAULT_WEIGHTS)
    assert sb.reallocated is True
    assert abs(sb.total - 100.0) < 1e-6
    assert "source_quality" in sb.neutral_dimensions


def test_reallocation_half_pass() -> None:
    checks = [_c("c1", "factuality", True), _c("c2", "factuality", False)]
    sb = score_from_checks(checks, DEFAULT_WEIGHTS)
    assert sb.reallocated is True
    assert abs(sb.total - 50.0) < 1e-6


def test_custom_weights_reallocation() -> None:
    checks = [_c("c1", "factuality", True), _c("c2", "factuality", False)]
    sb = score_from_checks(checks, {"factuality": 0.7, "citation": 0.3})
    # citation 无 check → 权重转给 factuality，归一化后 factuality 占 100%，通过一半 → 50
    assert abs(sb.total - 50.0) < 1e-6


def test_effective_weights_sum_one() -> None:
    eff = effective_weights(DEFAULT_WEIGHTS, ["source_quality", "citation"])
    assert abs(sum(eff.values()) - 1.0) < 1e-9
    assert eff.get("source_quality", 0) == 0
    assert eff.get("citation", 0) == 0


def test_dimension_ratios() -> None:
    checks = [_c("c1", "factuality", True), _c("c2", "factuality", False), _c("c3", "citation", None)]
    ratios, counts = dimension_ratios(checks)
    assert ratios["factuality"] == (0.5, 2.0)
    # citation 全为 None → 计 0，视为 neutral
    assert counts["citation"] == 0.0
    assert ratios["citation"][0] is None


def test_critical_failure_sets_hard_gate() -> None:
    checks = [_c("c1", "factuality", True), _c("c2", "factuality", False, critical=True)]
    assert hard_gate_failed(checks) is True
    # 有 critical 失败时 task_success 必须为 False
    sb = score_from_checks(checks, DEFAULT_WEIGHTS)
    assert task_success(sb.total, 60.0, checks, 0) is False


def test_compute_efficiency_zero_when_task_fails() -> None:
    score, neutral, reason = compute_efficiency(
        tool_calls=5, tool_limit=10, latency_seconds=5, time_limit=60,
        task_success_flag=False, timed_out=False,
    )
    assert score == 0.0
    assert neutral is False


def test_compute_efficiency_zero_when_timed_out() -> None:
    score, neutral, reason = compute_efficiency(
        tool_calls=5, tool_limit=10, latency_seconds=5, time_limit=60,
        task_success_flag=True, timed_out=True,
    )
    assert score == 0.0


def test_compute_efficiency_present_when_data_available() -> None:
    score, neutral, reason = compute_efficiency(
        tool_calls=5, tool_limit=10, latency_seconds=5, time_limit=100,
        task_success_flag=True, timed_out=False,
    )
    assert score is not None and score > 0
    assert neutral is False


def test_hard_gate_and_task_success() -> None:
    checks = [_c("c1", "factuality", True, critical=True)]
    assert task_success(80.0, 60.0, checks, 0) is True
    assert task_success(50.0, 60.0, checks, 0) is False  # 低于阈值
    assert task_success(80.0, 60.0, checks, 1) is False  # 有幻觉违规


def test_scoring_deterministic() -> None:
    checks = [_c("c1", "factuality", True), _c("c2", "completeness", False)]
    a = score_from_checks(checks, DEFAULT_WEIGHTS)
    b = score_from_checks(checks, DEFAULT_WEIGHTS)
    assert a.to_dict() == b.to_dict()
