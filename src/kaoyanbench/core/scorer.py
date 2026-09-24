"""Scorer：100 分制 7 维评分 + 维度中性重分配（方案 5.3 / B-17）。

核心函数 :func:`score_from_checks` 的语义**逐字按方案实现**：

对每个维度 ``d``：
    ``hit_ratio_d = Σ(passed==True 的 weight) / Σ(passed != None 的 weight)``
  - 该维度无任何可判定 check（分母 0）→ 视为 **neutral**：其权重按比例重分配给其余
    有检查的维度，``ScoreBreakdown.reallocated = True``，并在 ``neutral_dimensions`` 列出。
  - **全部维度均 neutral** → 抛 :class:`ScoringError`，不允许产出 100 分的假分数。

``tool_execution`` 不由 check 驱动：``= 10 * tool_success_rate``（``null`` → neutral）。

``efficiency``：
    ``5 * clamp01(1 - (0.5*tool_calls/tool_limit + 0.5*latency_seconds/time_limit))``
  且 ``task_success == False`` 时强制 0；``timed_out`` 时强制 0；
  ``tool_calls/tool_limit`` 或 ``latency/time_limit`` 任一不可用 → neutral。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from ..utils.hashing import sha256_text
from ..utils.text import clamp01, safe_str
from ..utils.timex import now_iso
from .errors import ScoringError
from .models import (
    DEFAULT_WEIGHTS,
    DIMENSION_MAX,
    DIMENSIONS,
    AgentOutput,
    CheckResult,
    ErrorItem,
    GradeResult,
    Metrics,
    ScoreBreakdown,
    Task,
)

__all__ = [
    "score_from_checks",
    "finalize_grade",
    "compute_metrics",
    "compute_efficiency",
    "compute_tool_execution",
    "hard_gate_failed",
    "task_success",
    "effective_weights",
]


def _round(value: float, digits: int = 4) -> float:
    return round(float(value), digits)


# --------------------------------------------------------------------------- #
# 维度分
# --------------------------------------------------------------------------- #
def dimension_ratios(
    checks: Sequence[CheckResult],
) -> tuple[dict[str, tuple[float | None, float]], dict[str, float]]:
    """按维度聚合 checks。

    返回 ``(ratios, available_weights)``：
      - ``ratios[d] = (hit_ratio | None, 可判定权重合计)``；``None`` 表示该维度 neutral。
    """
    passed_w: dict[str, float] = {d: 0.0 for d in DIMENSIONS}
    judged_w: dict[str, float] = {d: 0.0 for d in DIMENSIONS}
    for check in checks:
        dim = check.dimension if check.dimension in DIMENSIONS else "completeness"
        weight = float(check.weight) if check.weight is not None else 1.0
        if weight < 0:
            weight = 0.0
        if check.passed is None:
            continue
        judged_w[dim] += weight
        if check.passed:
            passed_w[dim] += weight

    ratios: dict[str, tuple[float | None, float]] = {}
    available: dict[str, float] = {}
    for dim in DIMENSIONS:
        if judged_w[dim] <= 0:
            ratios[dim] = (None, 0.0)
            available[dim] = 0.0
        else:
            ratios[dim] = (passed_w[dim] / judged_w[dim], judged_w[dim])
            available[dim] = judged_w[dim]
    return ratios, available


def effective_weights(
    weights: Mapping[str, float],
    neutral: Iterable[str],
) -> dict[str, float]:
    """把 neutral 维度的权重**按比例**转给其余维度；返回归一化后的权重。

    若没有可接收权重的维度（全部 neutral），返回空字典由调用方抛错。
    """
    neutral_set = {str(d) for d in neutral}
    base = {d: float(weights.get(d, 0.0) or 0.0) for d in DIMENSIONS}
    receivers = {d: w for d, w in base.items() if d not in neutral_set and w > 0}
    total_receiver = sum(receivers.values())
    if total_receiver <= 0:
        # 接收方权重全为 0 → 平均分配（避免除零；语义仍是「等比重分配」）
        receivers = {d: 1.0 for d in DIMENSIONS if d not in neutral_set}
        total_receiver = sum(receivers.values())
        if total_receiver <= 0:
            return {}
    released = sum(base[d] for d in neutral_set)
    factor = (total_receiver + released) / total_receiver
    result: dict[str, float] = {}
    for dim in DIMENSIONS:
        if dim in neutral_set:
            result[dim] = 0.0
        elif dim in receivers:
            result[dim] = receivers[dim] * factor
        else:
            result[dim] = base.get(dim, 0.0)
    return result


def compute_tool_execution(
    checks: Sequence[CheckResult],
    *,
    tool_success_rate: float | None,
    has_tool_checks: bool,
) -> tuple[float | None, bool]:
    """ToolExecution 维度：``10 * tool_success_rate``；``tool_success_rate is None`` → neutral。

    ``has_tool_checks`` 为 True 时说明任务显式声明了 tool_execution 维度的 check，
    此时以 checks 为准（不由 Runner 指标驱动），返回 ``(None, False)`` 让上层走 check 计算。
    """
    if has_tool_checks:
        return None, False
    if tool_success_rate is None:
        return None, True
    return _round(10.0 * clamp01(float(tool_success_rate))), False


def compute_efficiency(
    *,
    tool_calls: int | None,
    tool_limit: int | None,
    latency_seconds: float | None,
    time_limit: int | None,
    task_success_flag: bool,
    timed_out: bool,
) -> tuple[float | None, bool, str]:
    """Efficiency 维度。返回 ``(分数 | None, 是否 neutral, 说明)``。"""
    if timed_out:
        return 0.0, False, "timed_out=true，效率强制为 0"
    if not task_success_flag:
        return 0.0, False, "task_success=false，效率强制为 0"
    if tool_calls is None or not tool_limit or latency_seconds is None or not time_limit:
        return None, True, "tool_calls/tool_limit 或 latency/time_limit 不可用 → neutral"
    ratio = 0.5 * (float(tool_calls) / float(tool_limit)) + 0.5 * (
        float(latency_seconds) / float(time_limit)
    )
    score = 5.0 * clamp01(1.0 - ratio)
    return _round(score), False, f"资源使用率 {ratio:.3f} → 效率得分 {score:.2f}/5"


def score_from_checks(
    checks: list[CheckResult],
    weights: dict[str, float],
    total: int = 100,
    *,
    tool_success_rate: float | None = None,
    tool_calls: int | None = None,
    tool_limit: int | None = 60,
    latency_seconds: float | None = None,
    time_limit: int | None = 900,
    task_success_flag: bool = True,
    timed_out: bool = False,
) -> ScoreBreakdown:
    """按方案 5.3 计算 100 分制得分，含维度中性重分配。"""
    base_weights = {d: float(weights.get(d, DEFAULT_WEIGHTS.get(d, 0.0)) or 0.0) for d in DIMENSIONS}
    ratios, _available = dimension_ratios(checks)

    has_tool_checks = any(
        (c.dimension == "tool_execution" and c.passed is not None) for c in checks
    )
    tool_score, tool_neutral = compute_tool_execution(
        checks, tool_success_rate=tool_success_rate, has_tool_checks=has_tool_checks
    )
    efficiency_score, efficiency_neutral, _eff_detail = compute_efficiency(
        tool_calls=tool_calls,
        tool_limit=tool_limit,
        latency_seconds=latency_seconds,
        time_limit=time_limit,
        task_success_flag=task_success_flag,
        timed_out=timed_out,
    )

    # ★ 方案 5.3：若**没有任何维度由 check 驱动**（即全部 check-based 维度 neutral），
    #   直接抛 ScoringError，不允许用 Runner 指标凑出「假分数」。
    check_driven = [
        dim
        for dim in DIMENSIONS
        if dim not in ("tool_execution", "efficiency") and ratios[dim][0] is not None
    ]
    if not check_driven:
        raise ScoringError(
            "全部 7 个维度均为 neutral（无任何可判定的 check 项），无法计算总分；"
            "请为该任务补充 grader.checks（方案 5.3 不允许产出 100 分的假分数）"
        )

    neutral: list[str] = []
    for dim in DIMENSIONS:
        if dim == "tool_execution":
            if tool_neutral:
                neutral.append(dim)
            continue
        if dim == "efficiency":
            if efficiency_neutral:
                neutral.append(dim)
            continue
        if ratios[dim][0] is None:
            neutral.append(dim)

    if len(neutral) == len(DIMENSIONS):
        raise ScoringError(
            "全部 7 个维度均为 neutral（无任何可判定检查项），无法计算总分；"
            "请为该任务补充 check、或提供 tool_success_rate / latency 等运行指标"
        )

    effective = effective_weights(base_weights, neutral)
    if not effective:
        raise ScoringError("维度权重重分配失败：无任何可接收权重的维度")

    scale = float(total) / 100.0
    breakdown = ScoreBreakdown(
        reallocated=bool(neutral),
        neutral_dimensions=sorted(neutral),
        effective_weights={k: _round(v, 6) for k, v in effective.items()},
    )

    # 每个维度的「满分口径」：
    #   - 无重分配：严格等于该维度的默认满分（DIMENSION_MAX）。
    #   - 有重分配：把 neutral 维度的满分按比例转给其余维度，接收方总满分之和保持 == total。
    # 这样 total == Σ(满分_i × hit_ratio_i)，且 neutral 维度不贡献任何分数。
    if not neutral:
        max_scores = {dim: float(DIMENSION_MAX[dim]) * scale for dim in DIMENSIONS}
    else:
        max_scores = _reallocate_max_scores(neutral, effective, float(total))

    for dim in DIMENSIONS:
        if dim in neutral:
            setattr(breakdown, dim, 0.0)
        elif dim == "tool_execution":
            if has_tool_checks:
                # 有 tool_execution 维度的 check → 以 check 命中比例计分
                ratio = ratios[dim][0] or 0.0
                setattr(breakdown, dim, _round(max_scores[dim] * ratio, 4))
            else:
                setattr(breakdown, dim, _round(float(tool_score or 0.0) * scale, 4))
        elif dim == "efficiency":
            setattr(breakdown, dim, _round(float(efficiency_score or 0.0) * scale, 4))
        else:
            ratio = ratios[dim][0] or 0.0
            setattr(breakdown, dim, _round(max_scores[dim] * ratio, 4))

    breakdown.total = _round(sum(getattr(breakdown, dim) for dim in DIMENSIONS), 4)
    return breakdown


def _reallocate_max_scores(
    neutral: Sequence[str],
    effective: Mapping[str, float],
    total: float,
) -> dict[str, float]:
    """把 neutral 维度的满分按比例转给其余维度，接收方满分之和 == ``total``。

    分配依据：``effective`` 权重（已含重分配）中各维度的相对占比。
    ``tool_execution`` / ``efficiency`` 满分仍按其固有 10 / 5 分口径缩放，
    余下的分数按 effective 权重分配给 check 驱动维度。
    """
    neutral_set = set(neutral)
    fixed = {
        "tool_execution": float(DIMENSION_MAX["tool_execution"]),
        "efficiency": float(DIMENSION_MAX["efficiency"]),
    }
    fixed_total = sum(v for k, v in fixed.items() if k not in neutral_set)
    receivers = {
        dim: float(effective.get(dim, 0.0))
        for dim in DIMENSIONS
        if dim not in neutral_set and dim not in fixed
    }
    receiver_sum = sum(receivers.values())
    remaining = max(0.0, float(total) - fixed_total)
    out: dict[str, float] = {dim: 0.0 for dim in DIMENSIONS}
    for dim, weight in fixed.items():
        if dim not in neutral_set:
            out[dim] = weight
    if receiver_sum <= 0:
        # 没有 check 驱动接收方 → 全部分给 fixed 维度
        if fixed_total > 0:
            for dim in fixed:
                if dim not in neutral_set:
                    out[dim] = out[dim] / fixed_total * float(total)
        return out
    for dim, weight in receivers.items():
        out[dim] = remaining * (weight / receiver_sum)
    return out


# --------------------------------------------------------------------------- #
# task_success
# --------------------------------------------------------------------------- #
def hard_gate_failed(checks: Sequence[CheckResult]) -> bool:
    """critical check 是否有明确失败（``passed is False``）。"""
    return any(c.critical and c.passed is False for c in checks)


def task_success(
    score_total: float,
    threshold: float,
    checks: Sequence[CheckResult],
    hallucination_violations: int,
) -> bool:
    """方案 4.8：分数达标 **且** 无 critical 失败 **且** 零幻觉违规。"""
    if hallucination_violations > 0:
        return False
    if hard_gate_failed(checks):
        return False
    return float(score_total) >= float(threshold)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def compute_metrics(
    task: Task,
    output: AgentOutput,
    grade: GradeResult | None = None,
    *,
    checks: Sequence[CheckResult] | None = None,
    pass_threshold: float = 60.0,
) -> Metrics:
    """计算方案 4.8 的 11 项关键指标。

    ``null`` 语义严格遵守：拿不到就 ``None``，**绝不用 0 冒充**。
    """
    from .runner import usage_missing  # 局部导入避免循环

    check_list = list(checks if checks is not None else (grade.checks if grade else []))
    score_total = grade.score_total if grade is not None else 0.0

    # factuality：factuality 维度的加权命中率
    passed_w = 0.0
    judged_w = 0.0
    for check in check_list:
        if check.dimension != "factuality" or check.passed is None:
            continue
        judged_w += float(check.weight)
        if check.passed:
            passed_w += float(check.weight)
    factuality = (passed_w / judged_w) if judged_w > 0 else None

    # source_precision：E4/E5 占比
    sources = list(output.sources or [])
    official = [s for s in sources if str(s.evidence_level) in ("E4", "E5")]
    source_precision = (len(official) / len(sources)) if sources else None

    # search_recall：must_find 命中比例
    points = list(task.expected.must_find)
    if points:
        hits = 0
        haystack = _answer_text(output)
        for point in points:
            from ..utils.text import contains_all, contains_any

            if point.any_of or point.all_of:
                if contains_any(haystack, point.any_of) and (
                    not point.all_of or contains_all(haystack, point.all_of)
                ):
                    hits += 1
        search_recall = hits / len(points)
    else:
        search_recall = None

    # citation_accuracy：supported==true / supported!=null
    citations = list(output.citations or [])
    judged = [c for c in citations if c.supported is not None]
    citation_accuracy = (
        (sum(1 for c in judged if c.supported) / len(judged)) if judged else None
    )

    # hallucination_rate 与 citations_judged 等来自 GradeResult
    if grade is not None:
        violations = grade.hallucination_violations
        claims_checked = grade.citations_judged + len(
            [c for c in check_list if c.type == "must_not_claim" and c.passed is not None]
        )
    else:
        violations = sum(
            1 for c in check_list if c.type == "must_not_claim" and c.passed is False
        )
        claims_checked = len(
            [c for c in check_list if c.type == "must_not_claim" and c.passed is not None]
        )
    hallucination_rate = (violations / claims_checked) if claims_checked > 0 else 0.0

    # tool_success_rate
    tool_calls_detail = list(output.tool_calls or [])
    if tool_calls_detail:
        tool_success_rate = sum(1 for t in tool_calls_detail if t.ok) / len(tool_calls_detail)
    else:
        tool_success_rate = None

    usage = output.usage
    tokens = usage.total_tokens if usage and not usage_missing(usage) else None
    cost = usage.cost_usd if usage and not usage_missing(usage) else None
    latency_seconds = _round(max(0.0, float(output.duration_ms) / 1000.0), 3)

    success = task_success(
        score_total,
        task.grader.pass_threshold if task.grader.pass_threshold is not None else pass_threshold,
        check_list,
        violations,
    )

    return Metrics(
        task_success=success,
        factuality=_round(factuality, 4) if factuality is not None else None,
        source_precision=_round(source_precision, 4) if source_precision is not None else None,
        search_recall=_round(search_recall, 4) if search_recall is not None else None,
        citation_accuracy=_round(citation_accuracy, 4) if citation_accuracy is not None else None,
        hallucination_rate=_round(hallucination_rate, 4),
        tool_success_rate=_round(tool_success_rate, 4) if tool_success_rate is not None else None,
        tool_calls=len(tool_calls_detail),
        tokens=tokens,
        latency_seconds=latency_seconds,
        cost_usd=_round(cost, 6) if cost is not None else None,
    )


def _answer_text(output: AgentOutput) -> str:
    parts = [output.final_answer or ""]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 收尾：组装完整 GradeResult
# --------------------------------------------------------------------------- #
def finalize_grade(
    *,
    task: Task,
    output: AgentOutput,
    ctx: Any,
    checks: list[CheckResult],
    grader_type: str,
    grader_mode: str,
    degraded_reason: str | None,
    grader_versions: Mapping[str, Any] | None = None,
) -> GradeResult:
    """统一组装 :class:`GradeResult`（Scorer + Metrics + 错误分类）。"""
    from .errors import (  # 局部导入
        ClassificationInput,
        classify_or_unknown,
        finding_to_error_item,
    )

    weights = dict(ctx.weights or DEFAULT_WEIGHTS)
    # 先用「分数阈值以外」的信息判断 tool/latency 供 Efficiency 使用
    tool_rate = _tool_rate(output)
    latency_seconds = max(0.0, float(output.duration_ms) / 1000.0)

    # 第一遍：先算「不含 critical 判定」的 success 供 Efficiency 使用（避免循环依赖）
    provisional_success = not hard_gate_failed(checks) and not _has_hallucination(checks)
    score = score_from_checks(
        checks,
        weights,
        tool_success_rate=tool_rate,
        tool_calls=len(output.tool_calls or []),
        tool_limit=ctx.tool_limit,
        latency_seconds=latency_seconds,
        time_limit=ctx.time_limit,
        task_success_flag=provisional_success,
        timed_out=bool(output.timed_out),
    )

    citations = list(output.citations or [])
    judged = [c for c in citations if c.supported is not None]
    supported = sum(1 for c in judged if c.supported)
    violations = sum(
        1 for c in checks if c.type == "must_not_claim" and c.passed is False
    )

    # 正式 success 判定（使用最终 total）
    threshold = task.grader.pass_threshold if task.grader.pass_threshold else ctx.pass_threshold
    final_success = task_success(score.total, threshold, checks, violations)
    if final_success != provisional_success:
        # Efficiency 依赖 task_success → 需要重算一次（保持幂等：最多两轮）
        score = score_from_checks(
            checks,
            weights,
            tool_success_rate=tool_rate,
            tool_calls=len(output.tool_calls or []),
            tool_limit=ctx.tool_limit,
            latency_seconds=latency_seconds,
            time_limit=ctx.time_limit,
            task_success_flag=final_success,
            timed_out=bool(output.timed_out),
        )
        final_success = task_success(score.total, threshold, checks, violations)

    grade = GradeResult(
        task_id=task.task_id,
        run_id=output.run_id,
        grader_type=grader_type,
        grader_mode=grader_mode if grader_mode in ("full", "mixed", "degraded") else "full",
        degraded_reason=degraded_reason,
        score=score,
        checks=list(checks),
        citations_judged=len(judged),
        citations_supported=supported,
        citations_unjudged=len(citations) - len(judged),
        hallucination_violations=violations,
        graded_at=now_iso(getattr(ctx, "now", None)),
        grader_versions=dict(grader_versions or {}),
    )
    grade.metrics = compute_metrics(
        task, output, grade, checks=checks, pass_threshold=threshold
    )

    # 运行层错误（Runner 已产出的） + 分类器补充分类
    errors: list[ErrorItem] = list(output.errors or [])
    source = ClassificationInput(
        task_id=task.task_id,
        network=task.network,
        category=task.category,
        answer_format_kind=task.answer_format.kind,
        has_sources=bool(output.sources),
        required_sources=len(task.expected.required_sources),
        required_sources_satisfied=_required_sources_satisfied(task, output),
        source_precision=grade.metrics.source_precision,
        parse_failed=any(e.code == "PARSING_FAILURE" for e in output.errors),
        tool_calls=len(output.tool_calls or []),
        tool_success_rate=grade.metrics.tool_success_rate,
        planning_rules_present=any(c.type == "constraint" for c in checks),
        planning_rules_passed=not any(
            c.type == "constraint" and c.passed is False for c in checks
        ),
        canary_missed=_canary_missed(task, output),
        hallucination_violations=violations,
        ground_truth_conflict=_ground_truth_conflict(task, checks),
        citation_accuracy=grade.metrics.citation_accuracy,
        citation_count=len(citations),
        requires_citation=bool(task.expected.required_sources),
        numeric_check_failed=any(
            c.type in ("numeric", "file_json_match") and c.passed is False for c in checks
        ),
        timed_out=bool(output.timed_out),
    )
    existing_codes = {e.code for e in errors}
    for finding in classify_or_unknown(source):
        if finding.code in existing_codes:
            continue
        errors.append(
            finding_to_error_item(
                finding,
                task_id=task.task_id,
                run_id=output.run_id,
                at=now_iso(getattr(ctx, "now", None)),
                stage="grade",
            )
        )
        existing_codes.add(finding.code)
    grade.errors = errors
    return grade


def _tool_rate(output: AgentOutput) -> float | None:
    calls = list(output.tool_calls or [])
    if not calls:
        return None
    return sum(1 for c in calls if c.ok) / len(calls)


def _has_hallucination(checks: Sequence[CheckResult]) -> bool:
    return any(c.type == "must_not_claim" and c.passed is False for c in checks)


def _required_sources_satisfied(task: Task, output: AgentOutput) -> int:
    from .evidence import count_at_least, domain_matches

    satisfied = 0
    for req in task.expected.required_sources:
        hits = 0
        for source in output.sources or []:
            level_ok = _level_ge(source.evidence_level, req.level_min)
            domain_ok = (not req.domain_suffix) or domain_matches(source.domain, req.domain_suffix)
            if level_ok and domain_ok:
                hits += 1
        if hits >= max(1, int(req.min_count)):
            satisfied += 1
    return satisfied


def _level_ge(level: str, level_min: str) -> bool:
    order = {f"E{i}": i for i in range(6)}
    return order.get(str(level).upper(), 0) >= order.get(str(level_min).upper(), 0)


def _canary_missed(task: Task, output: AgentOutput) -> bool:
    """CONTEXT_FAILURE 的 canary 检测：指令里含「资料：<canary>」但答案完全不含该串。"""
    import re

    match = re.search(r"(?:canary|CANARY)[:：=]\s*([A-Za-z0-9_\-]{6,})", task.instruction or "")
    if not match:
        return False
    canary = match.group(1)
    return canary not in (output.final_answer or "")


def _ground_truth_conflict(task: Task, checks: Sequence[CheckResult]) -> bool:
    """ground_truth 存在且有 check 明确判否 → 视为与预期事实冲突。"""
    if not task.expected.ground_truth:
        return False
    return any(
        c.passed is False and c.type in ("numeric", "string_eq", "file_json_match")
        for c in checks
    )


def grade_fingerprint(task: Task, output: AgentOutput, grade: GradeResult) -> str:
    """评分指纹（用于「同输入同输出」断言与 store 幂等校验）。"""
    payload = "|".join(
        [
            task.task_id,
            output.run_id,
            safe_str(round(grade.score_total, 4)),
            safe_str(grade.grader_mode),
            ",".join(f"{c.id}:{c.passed}" for c in grade.checks),
        ]
    )
    return sha256_text(payload)[:16]
