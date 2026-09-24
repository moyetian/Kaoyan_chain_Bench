"""Reporter 协议与 ``build_report`` 编排（方案 5.6 / B-22 相关）。

**边界（重要）**：报告渲染（Markdown / HTML / SVG）由前端工程师负责，本模块
**不实现** 任何模板渲染，只负责：
1. 组装 :class:`~kaoyanbench.core.models.ReportModel`（唯一中间模型）；
2. 先写 ``report.json``（事实源，由后端实现 —— 它是事实源）；
3. 通过**懒加载 + 友好报错**的注册点把其余格式交给 ``kaoyanbench/reporters/*``；
4. 返回 :class:`ReportPaths`。

懒加载保证：前端文件尚未落地时，``import kaoyanbench.core.reporter`` **不会崩**，
只有真正请求该格式时才抛 :class:`ReporterNotFoundError`，并附上清晰的待办说明。
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..utils.text import safe_str
from ..utils.timex import now_iso
from .errors import ReporterNotFoundError
from .metrics import (
    _latest_grades_by_task,
    error_sample_tasks,
    grader_mode_summary,
    score_breakdown_mean,
)
from .models import (
    CATEGORY_LABELS,
    DIMENSION_MAX,
    DIMENSIONS,
    DIFFICULTY_LABELS,
    ERROR_CODES,
    ErrorItem,
    GradeResult,
    RegressionReport,
    ReportModel,
    Run,
    SuiteResult,
    Task,
)
from .regression import (
    gate_label,
    metric_label,
    summarize_verdict,
    threshold_text,
)

__all__ = [
    "SCHEMA_VERSION",
    "Reporter",
    "ReportPaths",
    "build_report",
    "build_report_model",
    "register_reporter",
    "available_reporters",
    "resolve_reporter",
    "write_json_report",
    "report_dir_name",
    "FORMAT_ALIASES",
]

SCHEMA_VERSION = "1.0"

#: 格式别名（CLI ``-f`` 用）。
FORMAT_ALIASES: dict[str, str] = {
    "json": "json",
    "md": "markdown",
    "markdown": "markdown",
    "html": "html",
    "csv": "csv",
}

#: 内置 Reporter 的懒加载路径（前端工程师负责实现这些模块）。
_LAZY_REPORTERS: dict[str, tuple[str, str]] = {
    "json": ("kaoyanbench.reporters.json_reporter", "JsonReporter"),
    "markdown": ("kaoyanbench.reporters.markdown_reporter", "MarkdownReporter"),
    "html": ("kaoyanbench.reporters.html_reporter", "HtmlReporter"),
    "csv": ("kaoyanbench.reporters.csv_reporter", "CsvReporter"),
}

#: 说明每种格式由谁负责（用于报错信息）。
_OWNERS: dict[str, str] = {
    "json": "后端（事实源）",
    "markdown": "前端工程师（F-02）",
    "html": "前端工程师（F-04）",
    "csv": "前端工程师（P1）",
}

_CUSTOM_REGISTRY: dict[str, Any] = {}


@runtime_checkable
class Reporter(Protocol):
    """报告渲染协议（方案 5.6）。"""

    name: str

    def render(self, report: ReportModel, out_dir: Path) -> Path:  # pragma: no cover - 协议
        ...


@dataclass
class ReportPaths:
    """三种格式的输出路径（未生成的为 ``None``）。"""

    json: Path | None = None
    markdown: Path | None = None
    html: Path | None = None
    csv: Path | None = None
    out_dir: Path | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "json": str(self.json) if self.json else None,
            "markdown": str(self.markdown) if self.markdown else None,
            "html": str(self.html) if self.html else None,
            "csv": str(self.csv) if self.csv else None,
            "out_dir": str(self.out_dir) if self.out_dir else None,
            "warnings": list(self.warnings),
        }

    def as_paths(self) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for key in ("json", "markdown", "html", "csv"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


def register_reporter(name: str, reporter: Any) -> None:
    """注册自定义 Reporter（第三方扩展）。"""
    _CUSTOM_REGISTRY[str(name)] = reporter


def available_reporters() -> list[str]:
    """当前**实际可导入**的 Reporter 格式（按内置顺序）。"""
    out: list[str] = []
    for name in _LAZY_REPORTERS:
        if name in _CUSTOM_REGISTRY or _try_import(name) is not None:
            out.append(name)
    for name in sorted(_CUSTOM_REGISTRY):
        if name not in out:
            out.append(name)
    return out


def resolve_reporter(name: str) -> Any:
    """解析 Reporter 实例。未实现时抛**可读**的 :class:`ReporterNotFoundError`。"""
    key = FORMAT_ALIASES.get(str(name).lower(), str(name).lower())
    if key in _CUSTOM_REGISTRY:
        return _CUSTOM_REGISTRY[key]
    loaded = _try_import(key)
    if loaded is not None:
        return loaded
    owner = _OWNERS.get(key, "未分配")
    raise ReporterNotFoundError(
        f"报告格式 '{key}' 暂不可用（负责方：{owner}）。"
        f"后端已实现 json（事实源）；markdown/html/csv 由前端工程师提供，"
        f"请确认 src/kaoyanbench/reporters/{key}_reporter.py 已就位。"
        f"当前可用：{', '.join(available_reporters()) or '（仅 json）'}"
    )


def _try_import(key: str) -> Any | None:
    """懒加载 Reporter 类；模块不存在 / 缺类 / 依赖报错 → 返回 ``None``（不 raise）。"""
    spec = _LAZY_REPORTERS.get(key)
    if spec is None:
        return None
    module_name, class_name = spec
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 - 前端模块自身的运行时错误不应拖垮核心
        return None
    cls = getattr(module, class_name, None)
    if cls is None:
        return None
    try:
        return cls()
    except Exception:  # noqa: BLE001
        return None


def report_dir_name(suite_id: str, agent: str, tag: str) -> str:
    """``reports/<suite_id>__<agent>__<tag>/``（方案 3.3）。"""
    safe = lambda s: str(s or "unknown").replace("/", "-").replace("\\", "-")  # noqa: E731
    return f"{safe(suite_id)}__{safe(agent)}__{safe(tag)}"


# --------------------------------------------------------------------------- #
# ReportModel 组装
# --------------------------------------------------------------------------- #
def build_report_model(
    suite_result: SuiteResult,
    baseline: SuiteResult | None = None,
    baseline_report: RegressionReport | None = None,
    *,
    now: str | None = None,
    task_meta: Mapping[str, Mapping[str, Any]] | None = None,
    trace_ref_template: str = "results/runs/{run_id}.jsonl",
) -> ReportModel:
    """组装方案 5.7 的 ``report.json`` 结构（**唯一中间模型**）。"""
    aggregates = suite_result.aggregates
    latest = _latest_grades_by_task(suite_result.grades)
    meta = dict(task_meta or {})

    summary: dict[str, Any] = {
        "task_success_rate": aggregates.task_success_rate,
        "failure_rate": aggregates.failure_rate,
        "pass_at_1": aggregates.pass_at_1,
        "pass_at_3": aggregates.pass_at_3,
        "score_mean": aggregates.score_mean,
        "score_median": aggregates.score_median,
        "score_p90": aggregates.score_p90,
        "factuality_mean": aggregates.factuality_mean,
        "source_precision_mean": aggregates.source_precision_mean,
        "search_recall_mean": aggregates.search_recall_mean,
        "citation_accuracy_mean": aggregates.citation_accuracy_mean,
        "hallucination_rate_mean": aggregates.hallucination_rate_mean,
        "tool_success_rate_mean": aggregates.tool_success_rate_mean,
        "tool_calls_mean": aggregates.tool_calls_mean,
        "tool_calls_p90": aggregates.tool_calls_p90,
        "tokens_mean": aggregates.tokens_mean,
        "tokens_p90": aggregates.tokens_p90,
        "latency_seconds_mean": aggregates.latency_seconds_mean,
        "latency_seconds_median": aggregates.latency_seconds_median,
        "latency_seconds_p90": aggregates.latency_seconds_p90,
        "cost_usd_mean": aggregates.cost_usd_mean,
        "cost_usd_total": aggregates.cost_usd_total,
        "usage_missing_count": aggregates.usage_missing_count,
        "usage_estimated_ratio": aggregates.usage_estimated_ratio,
    }

    breakdown = score_breakdown_mean(suite_result.grades)
    ordered_breakdown: dict[str, Any] = {dim: breakdown.get(dim) for dim in DIMENSIONS}
    ordered_breakdown["total"] = breakdown.get("total")
    for dim in DIMENSIONS:
        ordered_breakdown.setdefault(dim, None)

    errors = _error_breakdown(suite_result.grades, aggregates)
    tasks = _task_rows(suite_result, latest, meta, trace_ref_template)

    return ReportModel(
        schema_version=SCHEMA_VERSION,
        generated_at=now_iso(now),
        benchmark={
            "name": "KaoyanBench",
            "version": safe_str(suite_result.source.get("benchmark_version") or "1.0"),
        },
        suite={
            "id": suite_result.suite_id,
            "split": suite_result.split,
            "task_count": suite_result.task_count or len(tasks),
            "seed": suite_result.seed,
            "runs_per_task": suite_result.runs_per_task,
        },
        run={
            "agent": suite_result.agent,
            "agent_version": suite_result.agent_version,
            "model": suite_result.model,
            "tag": suite_result.tag,
            "offline_replay": suite_result.offline_replay,
            "grader_mode": aggregates.grader_mode,
            "degraded_task_count": aggregates.degraded_task_count,
            "environment": suite_result.environment.to_dict(),
        },
        summary=summary,
        score_breakdown_mean=ordered_breakdown,
        by_category=list(aggregates.by_category),
        by_difficulty=list(aggregates.by_difficulty),
        errors=errors,
        tasks=tasks,
        comparison=_comparison_block(baseline, suite_result, baseline_report),
    )


def _error_breakdown(
    grades: Sequence[GradeResult], aggregates: Any
) -> list[dict[str, Any]]:
    """11 类错误，**count=0 的也列出**（方案 5.7），按 count 降序。"""
    by_code = {item["code"]: item for item in (aggregates.error_counts or [])}
    rows: list[dict[str, Any]] = []
    for code in ERROR_CODES:
        item = by_code.get(code) or {"count": 0, "ratio": 0.0}
        rows.append(
            {
                "code": code,
                "count": int(item.get("count") or 0),
                "ratio": float(item.get("ratio") or 0.0),
                "sample_task_ids": error_sample_tasks(grades, code, limit=3),
            }
        )
    order = {code: idx for idx, code in enumerate(ERROR_CODES)}
    rows.sort(key=lambda r: (-int(r["count"]), order[str(r["code"])]))
    return rows


def _task_rows(
    suite_result: SuiteResult,
    latest: Mapping[str, GradeResult],
    meta: Mapping[str, Mapping[str, Any]],
    trace_ref_template: str,
) -> list[dict[str, Any]]:
    """``tasks[]`` 明细表数据源（长度 == task_count）。"""
    runs_by_task: dict[str, list[Run]] = {}
    for run in suite_result.runs:
        runs_by_task.setdefault(run.task_id, []).append(run)
    for key in runs_by_task:
        runs_by_task[key].sort(key=lambda r: (r.attempt, r.run_id))

    grades_by_task: dict[str, list[GradeResult]] = {}
    for grade in suite_result.grades:
        grades_by_task.setdefault(grade.task_id, []).append(grade)

    rows: list[dict[str, Any]] = []
    for task_id in sorted(latest):
        grade = latest[task_id]
        task_runs = runs_by_task.get(task_id, [])
        all_grades = sorted(grades_by_task.get(task_id, []), key=lambda g: g.run_id)
        attempts = [bool(g.metrics.task_success) for g in all_grades]
        task_meta = meta.get(task_id) or {}
        run_for_trace = task_runs[-1] if task_runs else None

        rows.append(
            {
                "task_id": task_id,
                "category": safe_str(task_meta.get("category") or "unknown"),
                "difficulty": safe_str(task_meta.get("difficulty") or "unknown"),
                "network": safe_str(task_meta.get("network") or "unknown"),
                "grader_type": grade.grader_type,
                "grader_mode": grade.grader_mode,
                "n_runs": len(all_grades) or max(1, len(task_runs)),
                "pass_at_1": attempts[0] if attempts else bool(grade.metrics.task_success),
                "pass_at_3": any(attempts[:3]) if attempts else bool(grade.metrics.task_success),
                "score": round(grade.score_total, 4),
                "metrics": {
                    "factuality": grade.metrics.factuality,
                    "source_precision": grade.metrics.source_precision,
                    "search_recall": grade.metrics.search_recall,
                    "citation_accuracy": grade.metrics.citation_accuracy,
                    "hallucination_rate": grade.metrics.hallucination_rate,
                    "tool_success_rate": grade.metrics.tool_success_rate,
                    "tool_calls": grade.metrics.tool_calls,
                    "tokens": grade.metrics.tokens,
                    "latency_seconds": grade.metrics.latency_seconds,
                    "cost_usd": grade.metrics.cost_usd,
                },
                "usage_source": _usage_source_of(run_for_trace),
                "checks": [
                    {
                        "id": c.id,
                        "type": c.type,
                        "dimension": c.dimension,
                        "passed": c.passed,
                        "critical": c.critical,
                        "detail": c.detail,
                    }
                    for c in grade.checks
                ],
                "error_codes": _unique_codes(grade.errors),
                "trace_ref": (
                    trace_ref_template.format(run_id=run_for_trace.run_id)
                    if run_for_trace is not None
                    else None
                ),
            }
        )
    return rows


def _usage_source_of(run: Run | None) -> str | None:
    if run is None or run.usage is None:
        return None
    return run.usage.usage_source


def _unique_codes(errors: Sequence[ErrorItem]) -> list[str]:
    out: list[str] = []
    for item in errors:
        if item.code and item.code not in out:
            out.append(item.code)
    return out


def _comparison_block(
    baseline: SuiteResult | None,
    current: SuiteResult,
    report: RegressionReport | None,
) -> dict[str, Any] | None:
    if report is None:
        return None
    deltas = [
        {
            "metric": delta.metric,
            "label": metric_label(delta.metric),
            "baseline": delta.baseline,
            "current": delta.current,
            "delta_pp": delta.delta_pp,
            "delta_abs": delta.delta_abs,
            "ratio": delta.ratio,
            "direction": delta.direction,
        }
        for delta in report.deltas
    ]
    gates = [
        {
            "name": gate.name,
            "label": gate_label(gate.name),
            "metric": gate.metric,
            "threshold_text": threshold_text(gate),
            "delta_pp": gate.delta_pp,
            "ratio": gate.ratio,
            "result": gate.result,
            "message": gate.message,
        }
        for gate in report.gates
    ]
    return {
        "baseline_tag": report.baseline_tag,
        "current_tag": report.current_tag or current.tag,
        "deltas": deltas,
        "gates": gates,
        "newly_failed_tasks": list(report.newly_failed_tasks),
        "newly_passed_tasks": list(report.newly_passed_tasks),
        "verdict": report.verdict,
        "verdict_label": summarize_verdict(report.verdict),
    }


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
def write_json_report(report: ReportModel, out_dir: Path) -> Path:
    """写 ``report.json``（**事实源**）。后端实现，前端不得覆盖。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.json"
    path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    return path


def build_report(
    suite_result: SuiteResult,
    baseline: SuiteResult | None = None,
    out_dir: Path | str = "reports",
    formats: Sequence[str] | None = None,
    *,
    now: str | None = None,
    baseline_report: RegressionReport | None = None,
    task_meta: Mapping[str, Mapping[str, Any]] | None = None,
    continue_on_missing: bool = True,
) -> ReportPaths:
    """方案 5.6 的编排：

    1. 组装 ``ReportModel``；
    2. **先写** ``report.json``（事实源）；
    3. 其余格式从 ``ReportModel`` 渲染（不二次读文件）；
    4. 返回 :class:`ReportPaths`。

    ``continue_on_missing=True`` 时，未实现的前端格式只记 warning 而不中断
    （保证后端可以独立跑通端到端）。设为 ``False`` 则在缺实现时抛
    :class:`ReporterNotFoundError`。
    """
    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    requested = [FORMAT_ALIASES.get(str(f).lower(), str(f).lower()) for f in (formats or ["json"])]

    model = build_report_model(
        suite_result,
        baseline,
        baseline_report,
        now=now,
        task_meta=task_meta,
    )
    paths = ReportPaths(out_dir=target_dir)

    # 1) 事实源先落地（无论 formats 是否包含 json）
    json_path = write_json_report(model, target_dir)
    paths.json = json_path

    # 2) 其余格式
    for fmt in requested:
        if fmt == "json":
            continue
        try:
            reporter = resolve_reporter(fmt)
        except ReporterNotFoundError as exc:
            if continue_on_missing:
                paths.warnings.append(exc.message)
                continue
            raise
        try:
            produced = Path(reporter.render(model, target_dir))
        except Exception as exc:  # noqa: BLE001 - 单个 reporter 崩溃不影响事实源
            paths.warnings.append(
                f"{fmt} 报告渲染失败（{type(exc).__name__}）：已保留 report.json 作为事实源"
            )
            continue
        setattr(paths, {"markdown": "markdown", "html": "html", "csv": "csv"}.get(fmt, fmt), produced)

    return paths


def load_report(path: str | Path) -> ReportModel:
    """读取 ``report.json`` 为 :class:`ReportModel`（带 schema 自检）。"""
    target = Path(path)
    if target.is_dir():
        target = target / "report.json"
    if not target.is_file():
        raise ReporterNotFoundError(f"report.json 不存在：{target.name}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ReporterNotFoundError("report.json 损坏，无法解析") from None
    model = ReportModel.from_dict(data)
    if model.schema_version != SCHEMA_VERSION:
        raise ReporterNotFoundError(
            f"report.json 的 schema_version={model.schema_version}，"
            f"当前 KaoyanBench 期望 {SCHEMA_VERSION}；请升级报告生成器"
        )
    return model


def dimension_max_of(dim: str) -> int:
    return int(DIMENSION_MAX.get(dim, 0))


def dimension_label(dim: str) -> str:
    from .models import DIMENSION_LABELS

    return DIMENSION_LABELS.get(dim, dim)


def category_label(key: str) -> str:
    return CATEGORY_LABELS.get(key, key)


def difficulty_label(key: str) -> str:
    return DIFFICULTY_LABELS.get(key, key)


def grader_mode_of(suite_result: SuiteResult) -> str:
    return grader_mode_summary(suite_result.grades)
