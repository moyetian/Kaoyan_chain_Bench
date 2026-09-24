"""CsvReporter：产出 ``report.csv``（方案 F 类 P1，任务明细表导出）。

用途：把 ``tasks[]`` 拉平成一行一任务的宽表，方便用 Excel / pandas 二次分析。
用 :mod:`csv` 标准库写入，**带 UTF-8 BOM**（``utf-8-sig``）以便 Windows Excel
直接双击不乱码；**不做**任何数值篡改，``None`` 原样留空（与后端 models.py
「采集不到一律 None、禁止用 0 冒充」的约定一致）。

同时输出一张 ``report.csv``，含表头行：
``task_id, category, difficulty, network, grader_type, grader_mode, n_runs,
pass_at_1, pass_at_3, score, factuality, source_precision, search_recall,
citation_accuracy, hallucination_rate, tool_success_rate, tool_calls, tokens,
latency_seconds, cost_usd, usage_source, error_codes, trace_ref``
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from ..core.models import ReportModel

__all__ = ["CsvReporter", "render_csv", "TASK_COLUMNS"]

#: ``tasks[]`` → 扁平表的列定义 ``(列名, 取值函数)``。
TASK_COLUMNS: tuple[tuple[str, Any], ...] = (
    ("task_id", lambda t: t.get("task_id")),
    ("category", lambda t: t.get("category")),
    ("difficulty", lambda t: t.get("difficulty")),
    ("network", lambda t: t.get("network")),
    ("grader_type", lambda t: t.get("grader_type")),
    ("grader_mode", lambda t: t.get("grader_mode")),
    ("n_runs", lambda t: t.get("n_runs")),
    ("pass_at_1", lambda t: t.get("pass_at_1")),
    ("pass_at_3", lambda t: t.get("pass_at_3")),
    ("score", lambda t: t.get("score")),
    ("factuality", lambda t: (t.get("metrics") or {}).get("factuality")),
    ("source_precision", lambda t: (t.get("metrics") or {}).get("source_precision")),
    ("search_recall", lambda t: (t.get("metrics") or {}).get("search_recall")),
    ("citation_accuracy", lambda t: (t.get("metrics") or {}).get("citation_accuracy")),
    ("hallucination_rate", lambda t: (t.get("metrics") or {}).get("hallucination_rate")),
    ("tool_success_rate", lambda t: (t.get("metrics") or {}).get("tool_success_rate")),
    ("tool_calls", lambda t: (t.get("metrics") or {}).get("tool_calls")),
    ("tokens", lambda t: (t.get("metrics") or {}).get("tokens")),
    ("latency_seconds", lambda t: (t.get("metrics") or {}).get("latency_seconds")),
    ("cost_usd", lambda t: (t.get("metrics") or {}).get("cost_usd")),
    ("usage_source", lambda t: t.get("usage_source")),
    ("error_codes", lambda t: "|".join(str(c) for c in (t.get("error_codes") or []))),
    ("checks_passed", lambda t: sum(1 for c in (t.get("checks") or []) if c.get("passed"))),
    ("checks_total", lambda t: len(t.get("checks") or [])),
    ("trace_ref", lambda t: t.get("trace_ref")),
)


def _cell(value: Any) -> str:
    """CSV 单元格：``None`` → 空串；布尔 → true/false（便于程序消费）；其余原样。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_csv(report: ReportModel) -> str:
    """把 ``tasks[]`` 渲染成 CSV 文本。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([name for name, _ in TASK_COLUMNS])
    for task in report.to_dict().get("tasks") or []:
        writer.writerow([_cell(getter(task)) for _, getter in TASK_COLUMNS])
    return buffer.getvalue()


class CsvReporter:
    """方案 5.6 的 Reporter 协议实现，产出 ``report.csv``。"""

    name = "csv"

    def render(self, report: ReportModel, out_dir: Path) -> Path:
        target_dir = Path(out_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "report.csv"
        # utf-8-sig（带 BOM）：Windows Excel 双击不乱码
        path.write_text(render_csv(report), encoding="utf-8-sig", newline="")
        return path


def _register() -> None:
    """自我注册到后端懒加载表（``core/reporter.py`` 已预留 ``csv`` 槽位）。"""
    try:
        from ..core.reporter import register_reporter

        register_reporter("csv", CsvReporter())
    except Exception:  # noqa: BLE001 - 注册失败不应拖垮模块导入
        pass


_register()


# --------------------------------------------------------------------------- #
# 自注册（方案 5.6 的注册点；不改动 core/reporter.py）
# --------------------------------------------------------------------------- #
def _register() -> None:
    """把 CsvReporter 注册进 ``core.reporter`` 的注册表（见 markdown_reporter 同名说明）。"""
    try:
        from ..core.reporter import register_reporter
    except Exception:  # noqa: BLE001
        return
    register_reporter("csv", CsvReporter())


_register()
