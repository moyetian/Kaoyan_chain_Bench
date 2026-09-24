"""JsonReporter：产出 ``report.json``（方案 F-01，**后端负责的事实源**）。

虽然是"报告"，但 ``report.json`` 是前端唯一输入，属于后端交付范围。
本 Reporter 只是对 :func:`kaoyanbench.core.reporter.write_json_report` 的协议适配，
保证 ``build_report`` 的统一调用方式一致。

自带 schema 自检：输出前校验 ``schema_version`` 与必需顶层键齐全。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.models import ReportModel

__all__ = ["JsonReporter", "REQUIRED_TOP_KEYS", "validate_report_dict"]

#: ``report.json`` 必需顶层键（方案 5.7）。
REQUIRED_TOP_KEYS: tuple[str, ...] = (
    "schema_version",
    "generated_at",
    "benchmark",
    "suite",
    "run",
    "summary",
    "score_breakdown_mean",
    "by_category",
    "by_difficulty",
    "errors",
    "tasks",
    "comparison",
)


def validate_report_dict(data: dict[str, Any]) -> list[str]:
    """校验 report.json 的顶层结构，返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    for key in REQUIRED_TOP_KEYS:
        if key not in data:
            problems.append(f"缺少顶层键：{key}")
    if data.get("schema_version") != "1.0":
        problems.append(f"schema_version 应为 '1.0'，实际 {data.get('schema_version')!r}")
    for section in ("benchmark", "suite", "run", "summary", "score_breakdown_mean"):
        if section in data and not isinstance(data[section], dict):
            problems.append(f"{section} 必须是对象")
    for section in ("by_category", "by_difficulty", "errors", "tasks"):
        if section in data and not isinstance(data[section], list):
            problems.append(f"{section} 必须是数组")
    return problems


class JsonReporter:
    """把 :class:`ReportModel` 写成 ``report.json``。"""

    name = "json"

    def render(self, report: ReportModel, out_dir: Path) -> Path:
        target_dir = Path(out_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        data = report.to_dict()
        problems = validate_report_dict(data)
        if problems:
            # 自检失败仍然写出（事实源优先），但把问题写进 warnings 字段便于排查
            data.setdefault("_schema_warnings", []).extend(problems)
        path = target_dir / "report.json"
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        return path
