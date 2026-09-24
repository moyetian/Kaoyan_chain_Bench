"""MarkdownReporter：产出 ``report.md``（方案 F-02）。

要求（验收标准）
----------------
- 含**总体指标表**、**按类别表**、**按难度表**、**错误分类表**、**TOP 失败任务清单**；
- 可直接贴 GitHub 渲染（GFM 表格、``<details>`` 折叠、无内联 HTML 依赖）；
- 后端给的 ``by_category[].label`` / ``by_difficulty[].label`` 优先使用（中文界面）；
- 空状态明确（``—`` 表示未采集，与后端 models.py 的 None 约定一致），
  列表为空时给出「暂无数据」而不是空表头。

与 ``report.json`` 的关系：**只消费** :class:`ReportModel`，不二次读文件（方案 5.6）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from ..core.models import (
    DEFAULT_WEIGHTS,
    DIMENSION_LABELS,
    DIMENSION_MAX,
    DIMENSIONS,
    ErrorItem,
    ReportModel,
)
from ..core.errors import ERROR_LABELS
from .assets import svg

__all__ = ["MarkdownReporter", "render_markdown"]

#: 指标卡定义 ``(summary 键, 中文标签, 单位说明)``（顺序即表格顺序，共 12 组核心指标）。
SUMMARY_ROWS: tuple[tuple[str, str], ...] = (
    ("task_success_rate", "任务成功率"),
    ("pass_at_1", "Pass@1"),
    ("pass_at_3", "Pass@3"),
    ("score_mean", "平均总分"),
    ("score_median", "总分中位数"),
    ("score_p90", "总分 P90"),
    ("factuality_mean", "事实性均值"),
    ("source_precision_mean", "来源精确率"),
    ("search_recall_mean", "检索召回率"),
    ("citation_accuracy_mean", "引用准确率"),
    ("hallucination_rate_mean", "幻觉率"),
    ("tool_success_rate_mean", "工具成功率"),
    ("tool_calls_mean", "工具调用均值"),
    ("tokens_mean", "Token 均值"),
    ("latency_seconds_mean", "平均耗时"),
    ("latency_seconds_p90", "耗时 P90"),
    ("cost_usd_mean", "平均成本"),
    ("cost_usd_total", "总成本"),
)

#: 成本估算相关说明键（在表格下方以脚注形式给出）。
USAGE_NOTE_KEYS = ("usage_missing_count", "usage_estimated_ratio")


def _esc(text: Any) -> str:
    """转义 Markdown 表格里的 ``|`` 与换行，避免撑破表格。"""
    raw = "" if text is None else str(text)
    return raw.replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def _num(value: Any, metric: str) -> str:
    return svg.fmt_value(value, metric)


def render_markdown(report: ReportModel) -> str:
    """把 :class:`ReportModel` 渲染成 Markdown 文本。"""
    data = report.to_dict()
    lines: list[str] = []
    add = lines.append

    suite = data.get("suite") or {}
    run = data.get("run") or {}
    summary = data.get("summary") or {}
    benchmark = data.get("benchmark") or {}

    # ------------------------------------------------------------------ 头部
    add(f"# {benchmark.get('name', 'KaoyanBench')} 评测报告")
    add("")
    add(
        f"> 基准版本 **v{_esc(benchmark.get('version', '1.0'))}** ｜ "
        f"任务集 **{_esc(suite.get('id', '-'))}**（split={_esc(suite.get('split', '-'))}，"
        f"{_esc(suite.get('task_count', 0))} 题 × {_esc(suite.get('runs_per_task', 1))} 次）"
        f" ｜ Agent **{_esc(run.get('agent', '-'))}** "
        f"{('v' + _esc(run.get('agent_version'))) if run.get('agent_version') else ''}"
    )
    add(
        f"> 模型 `{_esc(run.get('model', '-'))}` ｜ 版本标签 **{_esc(run.get('tag', '-'))}** ｜ "
        f"生成时间 {_esc(data.get('generated_at', '-'))}"
    )
    add("")

    # 降级告警（与 HTML 的 banner 对应）
    grader_mode = str(run.get("grader_mode", "full"))
    if grader_mode != "full":
        degraded = run.get("degraded_task_count") or 0
        add("> ## ⚠ 评分降级告警")
        add(
            f"> 本轮 `grader_mode = {_esc(grader_mode)}`，"
            f"**{_esc(degraded)} **个任务未使用完整语义评分，分数与事实性指标**不可直接跨版本对比**。"
        )
        add("")

    # ------------------------------------------------------------- 总体指标
    add("## 1. 总体指标")
    add("")
    add("| 指标 | 数值 | 说明 |")
    add("| --- | ---: | --- |")
    for key, label in SUMMARY_ROWS:
        value = summary.get(key)
        # `_mean` 键在 model 里存的是比率或原值，这里按 key 语义格式化
        note = _summary_note(key, value)
        add(f"| {label} | {_num(value, key)} | {note} |")
    add("")

    missing = summary.get("usage_missing_count")
    est_ratio = summary.get("usage_estimated_ratio")
    if missing or est_ratio:
        add(
            f"<sub>成本口径：{svg.fmt_int(missing)} 条运行未采集到 usage，"
            f"估算值占比 {svg.fmt_value(est_ratio, 'usage_estimated_ratio')}"
            f"（`usage_source` 见 `report.json`；跨 Agent 成本对比仅作参考）。</sub>"
        )
        add("")

    # --------------------------------------------------------- 总分构成
    add("## 2. 总分构成（7 维均值）")
    add("")
    breakdown = data.get("score_breakdown_mean") or {}
    ratio_scores, reallocated = _breakdown_ratios(data)
    add("| 维度 | 得分 | 默认满分 | 占 100 分比 |")
    add("| --- | ---: | ---: | ---: |")
    for dim in DIMENSIONS:
        value = breakdown.get(dim)
        add(
            f"| {DIMENSION_LABELS.get(dim, dim)} | {_fmt_score(value)} | "
            f"{DIMENSION_MAX.get(dim, 0)} | "
            f"{_pct(ratio_scores.get(dim))} |"
        )
    add(
        f"| **合计** | **{_fmt_score(breakdown.get('total'))}** | "
        f"**{sum(DIMENSION_MAX.values())}** | "
        f"**{_pct(1.0 if ratio_scores else None)}** |"
    )
    add("")
    if reallocated:
        add(
            "<sub>**口径说明（重要）**：本轮存在**维度中性再分配**"
            "（部分维度缺少检查项，其满分已按权重转给其余维度，"
            "后端 `ScoreBreakdown.reallocated = true`）。"
            "此时某些维度的得分会**超过方案 4.9 的默认满分**"
            "（如仅有事实性检查项时，事实性等效满分可达 95 分）。"
            "`report.json` 的 `score_breakdown_mean` **未透出逐维等效满分**"
            "（只在 `SuiteResult.grades[].score.effective_weights` 里），"
            "故本表用「**占 100 分比例**」作为跨版本可比口径，"
            "并保留默认满分列供对照；HTML 报告的堆叠条同理按占比绘制。</sub>"
        )
    else:
        add(
            "<sub>口径说明：无维度中性再分配，各维满分即方案 4.9 的默认满分"
            "（合计 100 分）。</sub>"
        )
    add("")

    # ------------------------------------------------------------- 按类别
    add("## 3. 按任务类别")
    add("")
    by_category = data.get("by_category") or []
    if by_category:
        add("| 类别 | 题数 | 成功率 | 平均分 | 事实性 | 引用准确率 | 检索召回 | 平均耗时 | 平均成本 |")
        add("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for row in by_category:
            add(
                "| {label} | {count} | {sr} | {score} | {fact} | {cit} | {recall} | {lat} | {cost} |".format(
                    label=_esc(row.get("label") or row.get("category") or "-"),
                    count=_esc(row.get("task_count", 0)),
                    sr=_num(row.get("task_success_rate"), "task_success_rate"),
                    score=_fmt_score(row.get("score_mean")),
                    fact=_num(row.get("factuality_mean"), "factuality_mean"),
                    cit=_num(row.get("citation_accuracy_mean"), "citation_accuracy_mean"),
                    recall=_num(row.get("search_recall_mean"), "search_recall_mean"),
                    lat=_num(row.get("latency_seconds_mean"), "latency_seconds_mean"),
                    cost=_num(row.get("cost_usd_mean"), "cost_usd_mean"),
                )
            )
    else:
        add("_暂无类别数据。_")
    add("")

    # ------------------------------------------------------------- 按难度
    add("## 4. 按难度")
    add("")
    by_difficulty = data.get("by_difficulty") or []
    if by_difficulty:
        add("| 难度 | 题数 | 成功率 | 平均分 | 事实性 | 平均耗时 |")
        add("| --- | ---: | ---: | ---: | ---: | ---: |")
        for row in by_difficulty:
            add(
                "| {label} | {count} | {sr} | {score} | {fact} | {lat} |".format(
                    label=_esc(row.get("label") or row.get("difficulty") or "-"),
                    count=_esc(row.get("task_count", 0)),
                    sr=_num(row.get("task_success_rate"), "task_success_rate"),
                    score=_fmt_score(row.get("score_mean")),
                    fact=_num(row.get("factuality_mean"), "factuality_mean"),
                    lat=_num(row.get("latency_seconds_mean"), "latency_seconds_mean"),
                )
            )
    else:
        add("_暂无难度数据。_")
    add("")

    # ------------------------------------------------------------- 错误分类
    add("## 5. 错误分类（11 类）")
    add("")
    errors = data.get("errors") or []
    if errors:
        add("| 错误码 | 分类 | 次数 | 占比 | 样例任务 |")
        add("| --- | --- | ---: | ---: | --- |")
        for row in errors:
            code = str(row.get("code") or "UNKNOWN")
            samples = row.get("sample_task_ids") or []
            add(
                "| `{code}` | {label} | {count} | {ratio} | {samples} |".format(
                    code=_esc(code),
                    label=_esc(ERROR_LABELS.get(code, code)),
                    count=svg.fmt_int(row.get("count")),
                    ratio=svg.fmt_value(row.get("ratio"), "failure_rate"),
                    samples="、".join(f"`{_esc(s)}`" for s in samples) or "—",
                )
            )
    else:
        add("_暂无错误数据。_")
    add("")

    # ------------------------------------------------- TOP 失败任务清单
    add("## 6. TOP 失败任务")
    add("")
    failed = _failed_tasks(data.get("tasks") or [])
    if failed:
        add("| # | 任务 | 类别 | 难度 | 得分 | 错误码 | 检查通过 |")
        add("| ---: | --- | --- | --- | ---: | --- | ---: |")
        for rank, task in enumerate(failed[:15], start=1):
            metrics = task.get("metrics") or {}
            checks = task.get("checks") or []
            passed = sum(1 for c in checks if c.get("passed"))
            codes = task.get("error_codes") or []
            add(
                "| {rank} | `{tid}` | {cat} | {diff} | {score} | {codes} | {passed}/{total} |".format(
                    rank=rank,
                    tid=_esc(task.get("task_id")),
                    cat=_esc(task.get("category")),
                    diff=_esc(task.get("difficulty")),
                    score=_fmt_score(task.get("score")),
                    codes="、".join(f"`{_esc(c)}`" for c in codes) or "—",
                    passed=passed,
                    total=len(checks),
                )
            )
            del metrics
        add("")
        add("<details><summary>展开：逐条检查明细（TOP 5）</summary>")
        add("")
        for task in failed[:5]:
            add(f"### `{_esc(task.get('task_id'))}`")
            add("")
            checks = task.get("checks") or []
            if checks:
                add("| 检查 | 类型 | 维度 | 结果 | 关键 | 详情 |")
                add("| --- | --- | --- | --- | --- | --- |")
                for check in checks:
                    add(
                        "| {cid} | `{type}` | {dim} | {result} | {critical} | {detail} |".format(
                            cid=_esc(check.get("id")),
                            type=_esc(check.get("type")),
                            dim=_esc(check.get("dimension") or "—"),
                            result="✅ 通过" if check.get("passed") else "❌ 未通过",
                            critical="是" if check.get("critical") else "否",
                            detail=_esc(check.get("detail") or ""),
                        )
                    )
            else:
                add("_该任务无检查项记录。_")
            trace = task.get("trace_ref")
            if trace:
                add("")
                add(f"追踪日志：`{_esc(trace)}`")
            add("")
        add("</details>")
    else:
        add("_本轮无失败任务。_")
    add("")

    # ------------------------------------------------------------- 版本对比
    comparison = data.get("comparison")
    if comparison:
        add("## 7. 版本对比")
        add("")
        add(
            f"> 对比基线 **{_esc(comparison.get('baseline_tag', '-'))}** → "
            f"当前 **{_esc(comparison.get('current_tag', '-'))}** ｜ "
            f"结论：**{_esc(comparison.get('verdict_label') or comparison.get('verdict') or '-')}**"
        )
        add("")
        deltas = comparison.get("deltas") or []
        if deltas:
            add("| 指标 | 基线 | 当前 | Δ | 方向 |")
            add("| --- | ---: | ---: | ---: | :---: |")
            for row in deltas:
                add(
                    "| {label} | {base} | {cur} | {delta} | {arrow} |".format(
                        label=_esc(row.get("label") or row.get("metric")),
                        base=_fmt_delta_side(row.get("baseline"), str(row.get("metric") or "")),
                        cur=_fmt_delta_side(row.get("current"), str(row.get("metric") or "")),
                        delta=svg.fmt_pp(row.get("delta_pp")),
                        arrow=_arrow(str(row.get("direction") or "flat")),
                    )
                )
        gates = comparison.get("gates") or []
        if gates:
            add("")
            add("| 门禁 | 阈值 | Δ | 结果 |")
            add("| --- | --- | ---: | :---: |")
            for gate in gates:
                add(
                    "| {label} | {threshold} | {delta} | {badge} |".format(
                        label=_esc(gate.get("label") or gate.get("name")),
                        threshold=_esc(gate.get("threshold_text") or "-"),
                        delta=svg.fmt_pp(gate.get("delta_pp")),
                        badge=_gate_badge(str(gate.get("result") or "skipped")),
                    )
                )
        newly_failed = comparison.get("newly_failed_tasks") or []
        newly_passed = comparison.get("newly_passed_tasks") or []
        if newly_failed or newly_passed:
            add("")
            if newly_failed:
                add("**新失败**：" + "、".join(f"`{_esc(t)}`" for t in newly_failed))
            if newly_passed:
                add("**新通过**：" + "、".join(f"`{_esc(t)}`" for t in newly_passed))
        add("")

    # ------------------------------------------------------------ 运行环境
    add("## 附录：运行环境")
    add("")
    env = run.get("environment") or {}
    add(f"- Python：`{_esc(env.get('python', '-'))}`")
    add(f"- 平台：`{_esc(env.get('platform', '-'))}`")
    add(f"- 容器：{'是' if env.get('docker') else '否'}")
    add(f"- 离线回放：{'是' if run.get('offline_replay') else '否'}")
    add(f"- 评分模式：`{_esc(grader_mode)}`（降级任务 {svg.fmt_int(run.get('degraded_task_count'))}）")
    add("")
    add("<sub>本文件由 KaoyanBench MarkdownReporter 生成，数据源为同目录 `report.json`（事实源）。</sub>")
    add("")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 内部助手
# --------------------------------------------------------------------------- #
def _fmt_score(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    return f"{float(value):.1f}"


def _fmt_max(value: Any) -> str:
    """满分口径展示：整数不带小数，非整数保留 1 位。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    number = float(value)
    if abs(number - round(number)) < 1e-9:
        return f"{int(round(number))}"
    return f"{number:.1f}"


def _pct(ratio: Any) -> str:
    """0~1 比率 → ``xx.x%``；``None`` → ``—``。"""
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        return "—"
    return f"{float(ratio) * 100:.1f}%"


def _breakdown_ratios(data: dict[str, Any]) -> tuple[dict[str, float], bool]:
    """把各维度得分换算成「占 100 分比例」，并判断是否存在维度中性再分配。

    背景（**需要向主理人回传的契约差异**）：
    ``report.json`` 的 ``score_breakdown_mean`` 只透出各维度**得分**与 ``total``，
    **没有**透出逐维等效满分。而 ``core/scorer.py`` 在存在 neutral 维度时会把
    neutral 维度的满分按权重转给其余维度（``ScoreBreakdown.reallocated = True``），
    因此某一维得分可超过方案 4.9 的 ``DIMENSION_MAX``
    （例如只有 ``factuality`` 检查项时其等效满分可达 95 分）。

    由于前端唯一输入是 ``report.json``，**无法**精确还原等效满分；
    这里改用**守恒口径**：

    - 每个维度的「占 100 分比」= ``该维得分 / total``（total 为 0 时用得分总和）；
    - 比值之和恒为 1，跨版本、跨任务都可比，且**绝不编造满分**。

    再分配判定：任一维度得分 > 其 ``DIMENSION_MAX``（容忍浮点误差）即认为发生。

    返回 ``(占比字典, 是否发生再分配)``。
    """
    breakdown = data.get("score_breakdown_mean") or {}
    total = breakdown.get("total")
    if not isinstance(total, (int, float)) or isinstance(total, bool) or total <= 0:
        total = sum(
            float(breakdown.get(dim))
            for dim in DIMENSIONS
            if isinstance(breakdown.get(dim), (int, float)) and not isinstance(breakdown.get(dim), bool)
        )
    reallocated = any(
        isinstance(breakdown.get(dim), (int, float))
        and not isinstance(breakdown.get(dim), bool)
        and float(breakdown[dim]) > float(DIMENSION_MAX.get(dim, 0)) + 1e-6
        for dim in DIMENSIONS
    )
    ratios: dict[str, float] = {}
    for dim in DIMENSIONS:
        value = breakdown.get(dim)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or total <= 0:
            continue
        ratios[dim] = float(value) / float(total)
    return ratios, reallocated


def _rate_of(value: Any, full: float) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not full:
        return "—"
    return f"{float(value) / full * 100:.1f}%"


def _summary_note(key: str, value: Any) -> str:
    """给指标一个简短的口径说明（未采集时明确写"未采集"，不用 0 冒充）。"""
    if value is None:
        return "未采集"
    notes = {
        "task_success_rate": "达标任务 / 全部任务",
        "pass_at_1": "第 1 次运行即通过的比例",
        "pass_at_3": "前 3 次内任一次通过的比例",
        "score_mean": "100 分制",
        "score_median": "100 分制",
        "score_p90": "100 分制",
        "factuality_mean": "仅统计有该维度检查的任务",
        "hallucination_rate_mean": "must_not_claim 命中率",
        "latency_seconds_mean": "端到端墙钟时间",
        "cost_usd_mean": "口径见脚注",
        "cost_usd_total": "口径见脚注",
        "usage_missing_count": "未采集 usage 的运行数",
    }
    return notes.get(key, "—")


def _failed_tasks(tasks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """失败任务按「分数升序 + task_id」稳定排序（分数最低的最值得关注）。"""

    def key(task: dict[str, Any]) -> tuple[float, str]:
        score = task.get("score")
        numeric = float(score) if isinstance(score, (int, float)) else 0.0
        return (numeric, str(task.get("task_id") or ""))

    failed = [t for t in tasks if not t.get("pass_at_1")]
    return sorted(failed, key=key)


def _fmt_delta_side(value: Any, metric: str) -> str:
    if value is None:
        return "—"
    if metric.endswith(("_rate", "_mean")) and isinstance(value, (int, float)) and abs(value) <= 1.0:
        return f"{float(value) * 100:.1f}%"
    return svg.fmt_value(value, metric)


def _arrow(direction: str) -> str:
    """中国习惯：**上升用红色 ▲、下降用绿色 ▼**（见 HTML 的配色约定）。"""
    return {"up": "▲ 上升", "down": "▼ 下降"}.get(direction, "— 持平")


def _gate_badge(result: str) -> str:
    return {
        "pass": "✅ PASS",
        "fail": "❌ FAIL",
        "warn": "⚠ WARN",
        "skipped": "➖ SKIPPED",
    }.get(result, f"➖ {result.upper()}")


class MarkdownReporter:
    """方案 5.6 的 Reporter 协议实现，产出 ``report.md``。"""

    name = "markdown"

    def render(self, report: ReportModel, out_dir: Path) -> Path:
        target_dir = Path(out_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "report.md"
        path.write_text(render_markdown(report), encoding="utf-8")
        return path


def _register() -> None:
    """自我注册到后端懒加载表（``core/reporter.py`` 已预留 ``markdown`` 槽位）。

    用延迟 import 避免与 ``core/reporter.py`` 形成循环依赖；
    失败时静默（后端仍有 ``_LAZY_REPORTERS`` 兜底，不影响渲染）。
    """
    try:
        from ..core.reporter import register_reporter

        register_reporter("markdown", MarkdownReporter())
    except Exception:  # noqa: BLE001 - 注册失败不应拖垮模块导入
        pass


_register()


# --------------------------------------------------------------------------- #
# 自注册（方案 5.6 的注册点；不改动 core/reporter.py）
# --------------------------------------------------------------------------- #
def _register() -> None:
    """把 MarkdownReporter 注册进 ``core.reporter`` 的注册表。

    后端 ``_LAZY_REPORTERS`` 已声明 ``markdown → markdown_reporter.MarkdownReporter``；
    这里额外调用 :func:`register_reporter` 让自定义注册表优先命中，
    从而在**不修改 core/reporter.py** 的前提下完成登记（符合「只新增不破坏」约束）。
    """
    try:
        from ..core.reporter import register_reporter
    except Exception:  # noqa: BLE001 - 核心缺失时不影响本模块被直接调用
        return
    register_reporter("markdown", MarkdownReporter())


_register()
