"""Regression：compare + gate + 退出码（方案 5.5 / 4.12 / B-21）。

★ 单位口径**写死**：所有「下降 >5%」指**绝对百分点（pp）**，不是相对百分比。
理由：比率型指标的相对变化在基线很低时会失真（0.02→0.03 相对 +50% 但绝对仅 +1pp）。

退出码：``pass → 0``，``warn → 0``（``--strict-warn`` 时为 1），``fail → 1``，运行错误 → 2。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from ..utils.text import safe_str
from ..utils.timex import now_iso
from .errors import RegressionError
from .metrics import compare_direction, mean_or_none
from .models import (
    Aggregates,
    GateResult,
    GradeResult,
    MetricDelta,
    RegressionReport,
    SuiteResult,
    parse_gate_config,
)

__all__ = [
    "DEFAULT_GATE_CONFIG",
    "COMPARABLE_RATIO_METRICS",
    "COMPARABLE_RELATIVE_METRICS",
    "compare",
    "evaluate_gate",
    "build_regression_report",
    "delta_for_metric",
    "verdict_from_gates",
    "exit_code_for",
    "load_gate_config",
]

#: 默认 Gate 配置（方案 5.5）。
DEFAULT_GATE_CONFIG: dict[str, Any] = {
    "mode": "absolute",
    "metrics": {
        "task_success_rate": {"max_delta_pp": -5.0, "max_ratio": None, "level": "fail"},
        "citation_accuracy_mean": {"max_delta_pp": -5.0, "max_ratio": None, "level": "fail"},
        "hallucination_rate_mean": {"max_delta_pp": 3.0, "max_ratio": None, "level": "fail"},
        "latency_seconds_p90": {"max_delta_pp": None, "max_ratio": 1.5, "level": "warn"},
    },
}

#: 比率型指标（0~1）：用 **pp** 比较，delta_pp = (current - baseline) × 100。
COMPARABLE_RATIO_METRICS: tuple[str, ...] = (
    "task_success_rate",
    "failure_rate",
    "pass_at_1",
    "pass_at_3",
    "factuality_mean",
    "source_precision_mean",
    "search_recall_mean",
    "citation_accuracy_mean",
    "hallucination_rate_mean",
    "tool_success_rate_mean",
    "usage_estimated_ratio",
)

#: 非比率指标（量纲型）：用 **ratio** 比较，delta_ratio = current / baseline。
COMPARABLE_RELATIVE_METRICS: tuple[str, ...] = (
    "score_mean",
    "score_median",
    "score_p90",
    "tool_calls_mean",
    "tool_calls_p90",
    "tokens_mean",
    "tokens_p90",
    "latency_seconds_mean",
    "latency_seconds_median",
    "latency_seconds_p90",
    "cost_usd_mean",
    "cost_usd_total",
)

#: 指标中文名（报告表头用）。
METRIC_LABELS: dict[str, str] = {
    "task_success_rate": "任务成功率",
    "failure_rate": "任务失败率",
    "pass_at_1": "Pass@1",
    "pass_at_3": "Pass@3",
    "score_mean": "平均分",
    "score_median": "中位分",
    "score_p90": "P90 分",
    "factuality_mean": "事实性均值",
    "source_precision_mean": "来源精确率均值",
    "search_recall_mean": "要点召回均值",
    "citation_accuracy_mean": "引用准确率均值",
    "hallucination_rate_mean": "幻觉率均值",
    "tool_success_rate_mean": "工具成功率均值",
    "tool_calls_mean": "平均工具调用数",
    "tool_calls_p90": "工具调用 P90",
    "tokens_mean": "平均 Token",
    "tokens_p90": "Token P90",
    "latency_seconds_mean": "平均耗时（秒）",
    "latency_seconds_median": "耗时中位数（秒）",
    "latency_seconds_p90": "耗时 P90（秒）",
    "cost_usd_mean": "平均成本（USD）",
    "cost_usd_total": "总成本（USD）",
    "usage_estimated_ratio": "Token 估算占比",
}

#: 「越低越好」的指标（方向判定用）。
LOWER_IS_BETTER: frozenset[str] = frozenset(
    {
        "failure_rate",
        "hallucination_rate_mean",
        "tool_calls_mean",
        "tool_calls_p90",
        "tokens_mean",
        "tokens_p90",
        "latency_seconds_mean",
        "latency_seconds_median",
        "latency_seconds_p90",
        "cost_usd_mean",
        "cost_usd_total",
    }
)


def load_gate_config(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """把 ``config/default.yaml`` 的 ``gates`` 段归一化；缺项用默认值补齐。"""
    parsed = parse_gate_config(raw)
    mode = parsed.get("mode", "absolute")
    metrics = dict(DEFAULT_GATE_CONFIG["metrics"])
    for name, value in (parsed.get("metrics") or {}).items():
        base = dict(metrics.get(name) or {"max_delta_pp": None, "max_ratio": None, "level": "fail"})
        for key in ("max_delta_pp", "max_ratio", "level"):
            if value.get(key) is not None:
                base[key] = value[key]
        metrics[name] = base
    return {"mode": mode, "metrics": metrics}


def _agg_value(aggregates: Aggregates | Mapping[str, Any] | None, metric: str) -> float | None:
    if aggregates is None:
        return None
    if isinstance(aggregates, Mapping):
        value = aggregates.get(metric)
    else:
        value = getattr(aggregates, metric, None)
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def delta_for_metric(
    metric: str,
    baseline: float | None,
    current: float | None,
) -> MetricDelta:
    """计算单个指标的 delta。比率型 → pp；量纲型 → ratio。"""
    if metric in COMPARABLE_RATIO_METRICS:
        kind = "ratio"
        delta_abs = (current - baseline) if (baseline is not None and current is not None) else None
        delta_pp = (delta_abs * 100.0) if delta_abs is not None else None
        ratio = (
            (current / baseline)
            if (baseline not in (None, 0.0) and current is not None)
            else None
        )
    else:
        kind = "relative"
        delta_abs = (current - baseline) if (baseline is not None and current is not None) else None
        delta_pp = None
        ratio = (
            (current / baseline)
            if (baseline not in (None, 0.0) and current is not None)
            else None
        )
    higher_better = metric not in LOWER_IS_BETTER
    return MetricDelta(
        metric=metric,
        baseline=baseline,
        current=current,
        delta_pp=round(delta_pp, 6) if delta_pp is not None else None,
        delta_abs=round(delta_abs, 6) if delta_abs is not None else None,
        ratio=round(ratio, 6) if ratio is not None else None,
        direction=compare_direction(delta_abs, higher_is_better=higher_better),
        kind=kind,
    )


# --------------------------------------------------------------------------- #
# compare
# --------------------------------------------------------------------------- #
def compare(baseline: SuiteResult, current: SuiteResult) -> list[MetricDelta]:
    """按 ``task_id`` 内连接对齐后比较顶层聚合指标。

    - 仅一侧存在的任务计入 ``*_missing_*``（由 :func:`build_regression_report` 统计），
      且**不参与** delta（避免用不同任务集比较产生误导）。
    - 比较全部可比指标；两侧都缺（null）的指标记 ``direction="flat"``。
    """
    deltas: list[MetricDelta] = []
    for metric in _all_metrics():
        base_value = _agg_value(baseline.aggregates, metric)
        current_value = _agg_value(current.aggregates, metric)
        deltas.append(delta_for_metric(metric, base_value, current_value))
    return deltas


def _all_metrics() -> list[str]:
    """全部可比指标（顺序固定：比率型在前，量纲型在后，各自按定义序）。"""
    return list(COMPARABLE_RATIO_METRICS) + list(COMPARABLE_RELATIVE_METRICS)


def _task_success_map(suite: SuiteResult) -> dict[str, bool]:
    """每个任务取 ``run_id`` 最大的 grade 作为代表（确定性）。"""
    latest: dict[str, GradeResult] = {}
    for grade in sorted(suite.grades, key=lambda g: (g.task_id, g.run_id)):
        latest[grade.task_id] = grade
    return {task_id: bool(g.metrics.task_success) for task_id, g in latest.items()}


def _aligned_success(
    baseline: SuiteResult, current: SuiteResult
) -> tuple[dict[str, bool], dict[str, bool], list[str], list[str]]:
    base_map = _task_success_map(baseline)
    cur_map = _task_success_map(current)
    base_ids = set(base_map)
    cur_ids = set(cur_map)
    common = sorted(base_ids & cur_ids)
    missing_in_current = sorted(base_ids - cur_ids)
    missing_in_baseline = sorted(cur_ids - base_ids)
    return (
        {tid: base_map[tid] for tid in common},
        {tid: cur_map[tid] for tid in common},
        missing_in_current,
        missing_in_baseline,
    )


# --------------------------------------------------------------------------- #
# gate
# --------------------------------------------------------------------------- #
def evaluate_gate(
    deltas: Sequence[MetricDelta],
    gates: Mapping[str, Any] | None = None,
) -> list[GateResult]:
    """按 Gate 配置判定。指标缺失（null）→ ``result='skipped'``。"""
    config = gates if gates and "metrics" in gates else load_gate_config(gates)
    mode = str(config.get("mode", "absolute") or "absolute")
    index = {delta.metric: delta for delta in deltas}
    results: list[GateResult] = []

    for name in sorted(config.get("metrics", {})):
        spec = config["metrics"][name]
        level = str(spec.get("level", "fail") or "fail")
        threshold_pp = spec.get("max_delta_pp")
        threshold_ratio = spec.get("max_ratio")
        delta = index.get(name)

        if delta is None or (delta.baseline is None and delta.current is None):
            results.append(
                GateResult(
                    name=name,
                    metric=name,
                    result="skipped",
                    threshold_delta_pp=threshold_pp,
                    threshold_ratio=threshold_ratio,
                    level=level,
                    message="该指标在 baseline 或 current 中缺失（null），无法判定",
                )
            )
            continue

        if threshold_pp is not None and mode == "absolute":
            if delta.delta_pp is None:
                results.append(
                    GateResult(
                        name=name,
                        metric=name,
                        result="skipped",
                        threshold_delta_pp=threshold_pp,
                        level=level,
                        message="delta_pp 无法计算（缺一侧数值）",
                    )
                )
                continue
            violated = _violates_pp(delta.delta_pp, float(threshold_pp))
            results.append(
                GateResult(
                    name=name,
                    metric=name,
                    result=("fail" if level == "fail" else "warn") if violated else "pass",
                    delta_pp=delta.delta_pp,
                    threshold_delta_pp=float(threshold_pp),
                    level=level,
                    message=(
                        f"Δ = {delta.delta_pp:+.2f}pp，阈值 {float(threshold_pp):+.1f}pp → "
                        f"{'触发' if violated else '未触发'}"
                    ),
                )
            )
            continue

        if threshold_ratio is not None:
            if delta.ratio is None:
                results.append(
                    GateResult(
                        name=name,
                        metric=name,
                        result="skipped",
                        threshold_ratio=float(threshold_ratio),
                        level=level,
                        message="ratio 无法计算（baseline 为 0 或缺值）",
                    )
                )
                continue
            violated = float(delta.ratio) > float(threshold_ratio)
            results.append(
                GateResult(
                    name=name,
                    metric=name,
                    result=("fail" if level == "fail" else "warn") if violated else "pass",
                    ratio=delta.ratio,
                    threshold_ratio=float(threshold_ratio),
                    level=level,
                    message=(
                        f"ratio = {delta.ratio:.3f}，阈值 {float(threshold_ratio):.2f} → "
                        f"{'触发' if violated else '未触发'}"
                    ),
                )
            )
            continue

        results.append(
            GateResult(
                name=name,
                metric=name,
                result="skipped",
                level=level,
                message="Gate 未配置阈值（max_delta_pp / max_ratio 均为空）",
            )
        )
    return results


def _violates_pp(delta_pp: float, threshold_pp: float) -> bool:
    """阈值方向由符号决定：负阈值 = 「下降超过 |阈值|」；正阈值 = 「上升超过阈值」。"""
    if threshold_pp < 0:
        return delta_pp < threshold_pp
    return delta_pp > threshold_pp


def verdict_from_gates(gates: Sequence[GateResult]) -> str:
    """任一 fail → fail；无 fail 但有 warn/skipped → warn；否则 pass。"""
    if any(g.result == "fail" for g in gates):
        return "fail"
    if any(g.result in ("warn", "skipped") for g in gates):
        return "warn"
    return "pass"


def exit_code_for(verdict: str, *, strict_warn: bool = False) -> int:
    """退出码：pass→0；warn→0（strict_warn 时 1）；fail→1。"""
    if verdict == "fail":
        return 1
    if verdict == "warn" and strict_warn:
        return 1
    return 0


# --------------------------------------------------------------------------- #
# 组装报告
# --------------------------------------------------------------------------- #
def build_regression_report(
    baseline: SuiteResult,
    current: SuiteResult,
    gates: Mapping[str, Any] | None = None,
    *,
    now: str | None = None,
    strict_warn: bool = False,
    already_compared: Sequence[MetricDelta] | None = None,
) -> RegressionReport:
    """组装方案 4.12 的 :class:`RegressionReport`（含 verdict 与 exit_code）。"""
    if baseline is None or current is None:
        raise RegressionError("回归比对需要 baseline 与 current 两份 suite 结果")

    base_map, cur_map, missing_in_current, missing_in_baseline = _aligned_success(baseline, current)
    n_compared = len(base_map)

    deltas = list(already_compared) if already_compared is not None else compare(baseline, current)

    # 若任务集不一致，重算「仅共有任务」的 task_success_rate，避免缺题导致误导
    if missing_in_current or missing_in_baseline:
        aligned_base = sum(1 for v in base_map.values() if v) / n_compared if n_compared else None
        aligned_cur = sum(1 for v in cur_map.values() if v) / n_compared if n_compared else None
        for position, delta in enumerate(deltas):
            if delta.metric == "task_success_rate":
                deltas[position] = delta_for_metric("task_success_rate", aligned_base, aligned_cur)
                break

    gate_results = evaluate_gate(deltas, gates)
    # v1.1 可比性门禁（对标 DeepEval judge 固定原则）：grader_mode 不一致
    # （full vs degraded/mixed）时分数不可比，追加 warn 门禁，提示以
    # deterministic 子集或同 mode 重跑为准；绝不静默比较。
    try:
        from .metrics import grader_mode_summary

        base_mode = grader_mode_summary(list(baseline.grades or []))
        cur_mode = grader_mode_summary(list(current.grades or []))
    except Exception:  # noqa: BLE001 - 门禁缺失不阻断主 verdict
        base_mode = cur_mode = None
    if base_mode and cur_mode and base_mode != cur_mode:
        from .models import GateResult as _GateResult

        gate_results.append(
            _GateResult(
                name="grader_mode_mismatch",
                metric="grader_mode",
                result="warn",
                level="warn",
                message=(
                    f"grader_mode 不一致（baseline={base_mode}, current={cur_mode}），"
                    "含 degraded 的分数不可直接比较；建议同 mode 重跑或只看 deterministic 任务"
                ),
            )
        )
    verdict = verdict_from_gates(gate_results)

    newly_failed = sorted(tid for tid in base_map if base_map[tid] and not cur_map[tid])
    newly_passed = sorted(tid for tid in base_map if not base_map[tid] and cur_map[tid])

    return RegressionReport(
        schema_version="1.0",
        baseline_tag=baseline.tag,
        current_tag=current.tag,
        suite_id=current.suite_id or baseline.suite_id,
        compared_at=now_iso(now),
        n_tasks_compared=n_compared,
        n_tasks_missing_in_current=len(missing_in_current),
        n_tasks_missing_in_baseline=len(missing_in_baseline),
        deltas=deltas,
        gates=gate_results,
        newly_failed_tasks=newly_failed,
        newly_passed_tasks=newly_passed,
        verdict=verdict,
        exit_code=exit_code_for(verdict, strict_warn=strict_warn),
    )


def metric_label(metric: str) -> str:
    return METRIC_LABELS.get(metric, metric)


def threshold_text(gate: GateResult) -> str:
    """给报告用的阈值说明（方案 5.7 的 ``threshold_text``）。"""
    if gate.threshold_delta_pp is not None:
        value = float(gate.threshold_delta_pp)
        host = gate.metric
        if value < 0:
            return f"{metric_label(host)} 下降 > {abs(value):.0f}pp 则 {gate.level.upper()}"
        return f"{metric_label(host)} 上升 > {value:.0f}pp 则 {gate.level.upper()}"
    if gate.threshold_ratio is not None:
        return f"{metric_label(gate.metric)} 相对基线 > {float(gate.threshold_ratio):.2f}× 则 {gate.level.upper()}"
    return "未配置阈值"


def gate_label(name: str) -> str:
    mapping = {
        "task_success_rate": "任务成功率门禁",
        "citation_accuracy_mean": "引用准确率门禁",
        "hallucination_rate_mean": "幻觉率门禁",
        "latency_seconds_p90": "耗时 P90 门禁",
    }
    return mapping.get(name, f"{metric_label(name)}门禁")


def summarize_verdict(verdict: str) -> str:
    return {"pass": "通过", "warn": "告警", "fail": "未通过"}.get(verdict, verdict)


def _unused(_: Any) -> None:  # pragma: no cover
    _ = (safe_str, mean_or_none, Iterable)
