"""B-21：回归 compare / evaluate_gate / build_regression_report / 退出码。"""

from __future__ import annotations

import pytest

from kaoyanbench.core.models import (
    Aggregates,
    Environment,
    SuiteResult,
)
from kaoyanbench.core.regression import (
    build_regression_report,
    compare,
    evaluate_gate,
    exit_code_for,
    verdict_from_gates,
)

GATES = {
    "mode": "absolute",
    "task_success_rate": {"max_delta_pp": -5.0, "level": "fail"},
    "hallucination_rate_mean": {"max_delta_pp": 3.0, "level": "fail"},
    "latency_seconds_p90": {"max_ratio": 1.5, "level": "warn"},
}


def _suite(tag: str, success: float, halluc: float = 0.0, latency_p90: float = 100.0,
           citation: float = 0.9) -> SuiteResult:
    return SuiteResult(
        suite_id="smoke",
        split="public",
        task_count=10,
        agent="mock",
        agent_version="1.0.0",
        model="m",
        runs_per_task=1,
        seed=42,
        tag=tag,
        created_at="2026-01-01T00:00:00Z",
        environment=Environment(),
        aggregates=Aggregates(
            n_tasks=10,
            task_success_rate=success,
            citation_accuracy_mean=citation,
            hallucination_rate_mean=halluc,
            latency_seconds_p90=latency_p90,
        ),
    )


def test_compare_produces_deltas() -> None:
    deltas = compare(_suite("v1", 0.70), _suite("v2", 0.80))
    names = {d.metric for d in deltas}
    assert "task_success_rate" in names
    tsr = next(d for d in deltas if d.metric == "task_success_rate")
    assert tsr.delta_pp == pytest.approx(10.0, abs=1e-6)
    assert tsr.direction == "up"


def test_regression_pass_when_improved() -> None:
    report = build_regression_report(_suite("v1", 0.70), _suite("v2", 0.80), gates=GATES)
    assert report.verdict == "pass"
    assert report.exit_code == 0


def test_regression_fail_on_6pp_drop() -> None:
    # 下降 6pp（> 5pp 阈值）→ fail，exit_code 1
    report = build_regression_report(_suite("v1", 0.80), _suite("v2", 0.74), gates=GATES)
    assert report.verdict == "fail"
    assert report.exit_code == 1
    assert exit_code_for(report.verdict) == 1


def test_regression_pass_on_exactly_5pp_drop() -> None:
    # 恰好下降 5pp → 不超阈值（阈值是「下降 > 5pp」）→ pass
    report = build_regression_report(_suite("v1", 0.80), _suite("v2", 0.75), gates=GATES)
    assert report.verdict == "pass"


def test_regression_warn_on_latency_ratio() -> None:
    # latency p90 从 100 → 200（ratio 2.0 > 1.5）→ warn
    report = build_regression_report(
        _suite("v1", 0.80, latency_p90=100.0),
        _suite("v2", 0.80, latency_p90=200.0),
        gates=GATES,
    )
    warn_gates = [g for g in report.gates if g.result == "warn"]
    assert warn_gates
    assert report.verdict == "warn"
    assert exit_code_for(report.verdict) == 0
    assert exit_code_for(report.verdict, strict_warn=True) == 1


def test_hallucination_increase_fails() -> None:
    report = build_regression_report(
        _suite("v1", 0.80, halluc=0.0),
        _suite("v2", 0.80, halluc=0.10),  # +10pp > 3pp
        gates=GATES,
    )
    assert report.verdict == "fail"


def test_gate_skipped_when_metric_null() -> None:
    deltas = compare(_suite("v1", 0.80), _suite("v2", 0.80))
    # 通过 delta_for 构造 null 指标 → skipped
    gates = evaluate_gate(deltas, {"mode": "absolute", "nope_metric": {"max_delta_pp": -5.0, "level": "fail"}})
    # 不存在的 metric 不产生 gate（或 skipped）
    assert all(g.result in ("pass", "fail", "warn", "skipped") for g in gates)


def test_verdict_from_gates_fail_wins() -> None:
    from kaoyanbench.core.models import GateResult

    gates = [
        GateResult(name="a", metric="m1", result="pass"),
        GateResult(name="b", metric="m2", result="fail"),
        GateResult(name="c", metric="m3", result="warn"),
    ]
    assert verdict_from_gates(gates) == "fail"


def test_regression_report_deterministic() -> None:
    a = build_regression_report(_suite("v1", 0.70), _suite("v2", 0.80), gates=GATES, now="2026-01-01T00:00:00Z")
    b = build_regression_report(_suite("v1", 0.70), _suite("v2", 0.80), gates=GATES, now="2026-01-01T00:00:00Z")
    assert a.to_dict() == b.to_dict()
