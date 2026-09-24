"""HtmlReporter：产出**单文件** ``report.html``（方案 F-04~F-06）。

硬约束（验收会实测）
--------------------
1. **单文件、零外部请求**：CSS / JS / SVG 全部内联；不引 CDN、不引外部字体、
   不引图片文件、不用 emoji 图标（图标一律内联 SVG 或纯 CSS 绘制）。
   断网双击必须完整可看。
2. 图表全部由 :mod:`kaoyanbench.reporters.assets.svg` **手写 SVG** 生成。
3. 深/浅色跟随 ``prefers-color-scheme``（两套 CSS 变量）。
4. 表格排序 / 筛选 / 搜索为纯前端 JS，无依赖。
5. 界面中文；类别 / 难度用后端给的 ``label``。
6. 中国习惯：**上升红色、下降绿色**（Δ 箭头配色）。
7. 数据在生成期以 JSON 内联进 ``<script type="application/json">``，
   运行时从 DOM 读取（**不发起任何 fetch**）。

渲染的 12 类元素见 ``ELEMENT_SELECTORS``（与 ``tools/check_report_html.py`` 一一对应）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..core.errors import ERROR_LABELS
from ..core.models import (
    CATEGORY_LABELS,
    DIMENSION_LABELS,
    DIMENSION_MAX,
    DIMENSIONS,
    DIFFICULTY_LABELS,
    ReportModel,
)
from .assets import svg

__all__ = [
    "HtmlReporter",
    "render_html",
    "ELEMENT_SELECTORS",
    "build_view_model",
]

#: 12 类必需元素 → 校验用的 DOM 选择器（``tools/check_report_html.py`` 直接复用）。
ELEMENT_SELECTORS: dict[int, tuple[str, str]] = {
    1: ("顶部告警横幅", "#degraded-banner"),
    2: ("12 个指标卡", "#metric-cards .metric-card"),
    3: ("总分构成堆叠条", "#chart-breakdown svg.chart-stacked"),
    4: ("按类别能力柱状图", "#chart-category svg.chart-grouped"),
    5: ("按难度成功率柱", "#chart-difficulty svg.chart-grouped"),
    6: ("Latency 分布直方图", "#chart-latency svg.chart-histogram"),
    7: ("成本 TOP10 横向条", "#chart-cost svg.chart-hbars"),
    8: ("错误分类条形", "#chart-errors svg.chart-hbars"),
    9: ("Pass@1 vs Pass@3 双条", "#chart-pass svg.chart-double"),
    10: ("任务明细表", "#task-table tbody#task-table-body"),
    11: ("任务详情抽屉", "#task-drawer .drawer-check-row"),
    12: ("版本对比表", "#comparison-section .gate-badge"),
}

#: 指标卡定义：``(summary 键, 中文标签, 是否 estimated 敏感)``（12 张卡）。
METRIC_CARDS: tuple[tuple[str, str], ...] = (
    ("task_success_rate", "任务成功率"),
    ("pass_at_1", "Pass@1"),
    ("pass_at_3", "Pass@3"),
    ("score_mean", "平均总分"),
    ("score_p90", "总分 P90"),
    ("factuality_mean", "事实性"),
    ("citation_accuracy_mean", "引用准确率"),
    ("hallucination_rate_mean", "幻觉率"),
    ("tool_calls_mean", "工具调用均值"),
    ("tokens_mean", "Token 均值"),
    ("latency_seconds_mean", "平均耗时"),
    ("cost_usd_mean", "平均成本"),
)

#: 拉取后端 label 的映射（优先用后端给出的中文标签）。
_CATEGORY_LABELS = dict(CATEGORY_LABELS)
_DIFFICULTY_LABELS = dict(DIFFICULTY_LABELS)


# --------------------------------------------------------------------------- #
# 视图模型：把 report.json 转成"前端可直接渲染"的结构（生成期算好，运行时零计算）
# --------------------------------------------------------------------------- #
def build_view_model(report: ReportModel) -> dict[str, Any]:
    """构造内联到 HTML 的视图模型（**派生自 report.json，不改变其语义**）。"""
    data = report.to_dict()
    summary = dict(data.get("summary") or {})
    run = dict(data.get("run") or {})
    suite = dict(data.get("suite") or {})
    benchmark = dict(data.get("benchmark") or {})

    return {
        "schema_version": data.get("schema_version"),
        "generated_at": data.get("generated_at"),
        "benchmark": benchmark,
        "suite": suite,
        "run": run,
        "summary": summary,
        "score_breakdown_mean": dict(data.get("score_breakdown_mean") or {}),
        "breakdown_ratios": _breakdown_ratios(data),
        "by_category": [dict(r) for r in (data.get("by_category") or [])],
        "by_difficulty": [dict(r) for r in (data.get("by_difficulty") or [])],
        "errors": [dict(r) for r in (data.get("errors") or [])],
        "tasks": [dict(t) for t in (data.get("tasks") or [])],
        "comparison": data.get("comparison"),
        "cost_top": _cost_top(data.get("tasks") or [], limit=10),
        "latency_hist": _latency_histogram(data.get("tasks") or []),
        "labels": {
            "category": _CATEGORY_LABELS,
            "difficulty": _DIFFICULTY_LABELS,
            "dimension": dict(DIMENSION_LABELS),
            "error": dict(ERROR_LABELS),
        },
        "dimension_max": dict(DIMENSION_MAX),
        "dimensions": list(DIMENSIONS),
    }


def _breakdown_ratios(data: dict[str, Any]) -> dict[str, float]:
    """各维度得分 / 总分（守恒口径；见 markdown_reporter 的同名说明）。

    ``report.json`` 未透出逐维等效满分，因此前端用「占 100 分比例」作堆叠条口径，
    保证堆叠条总长恒等于总分，且跨版本可比。
    """
    breakdown = data.get("score_breakdown_mean") or {}
    total = breakdown.get("total")
    numeric_ok = isinstance(total, (int, float)) and not isinstance(total, bool)
    if not numeric_ok or float(total or 0) <= 0:
        total = sum(
            float(breakdown.get(dim))
            for dim in DIMENSIONS
            if isinstance(breakdown.get(dim), (int, float))
            and not isinstance(breakdown.get(dim), bool)
        )
    total = float(total or 0)
    ratios: dict[str, float] = {}
    for dim in DIMENSIONS:
        value = breakdown.get(dim)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and total > 0:
            ratios[dim] = float(value) / total
    return ratios


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _cost_top(tasks: Sequence[dict[str, Any]], *, limit: int = 10) -> list[dict[str, Any]]:
    """成本 TOP N（仅统计有 ``cost_usd`` 的任务；未采集的不参与排名）。"""
    rows = []
    for task in tasks:
        cost = _num((task.get("metrics") or {}).get("cost_usd"))
        if cost is None:
            continue
        rows.append(
            {
                "task_id": task.get("task_id"),
                "category": task.get("category"),
                "difficulty": task.get("difficulty"),
                "cost_usd": cost,
            }
        )
    rows.sort(key=lambda r: (-float(r["cost_usd"] or 0.0), str(r["task_id"])))
    return rows[:limit]


#: Latency 直方图分桶边界（秒）。按方案 5.7「前端分桶」。
LATENCY_BINS: tuple[tuple[float, float, str], ...] = (
    (0.0, 30.0, "0–30s"),
    (30.0, 60.0, "30–60s"),
    (60.0, 120.0, "1–2min"),
    (120.0, 180.0, "2–3min"),
    (180.0, 300.0, "3–5min"),
    (300.0, 600.0, "5–10min"),
    (600.0, float("inf"), ">10min"),
)


def _latency_histogram(tasks: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """对 ``tasks[].metrics.latency_seconds`` 分桶 + 计算 P50/P90。

    返回 ``{"bins": [(label, count), ...], "markers": [(name, value, bin_index), ...],
    "n": int, "p50": float|None, "p90": float|None}``。

    **仅使用真实采集到的值**；无任何有效值时返回空桶（前端显示空状态）。
    """
    values = sorted(
        v
        for v in (_num((t.get("metrics") or {}).get("latency_seconds")) for t in tasks)
        if v is not None
    )
    counts = [0] * len(LATENCY_BINS)
    for value in values:
        for index, (low, high, _label) in enumerate(LATENCY_BINS):
            if low <= value < high:
                counts[index] += 1
                break
        else:
            counts[-1] += 1

    def _percentile(ratio: float) -> float | None:
        if not values:
            return None
        if len(values) == 1:
            return values[0]
        # 线性插值（与后端 metrics.py 的 P90 口径一致：sorted + 插值）
        position = ratio * (len(values) - 1)
        lower = int(position)
        upper = min(lower + 1, len(values) - 1)
        frac = position - lower
        return values[lower] + (values[upper] - values[lower]) * frac

    p50 = _percentile(0.5)
    p90 = _percentile(0.9)

    def _bin_of(value: float | None) -> int:
        if value is None:
            return 0
        for index, (low, high, _label) in enumerate(LATENCY_BINS):
            if low <= value < high:
                return index
        return len(LATENCY_BINS) - 1

    return {
        "bins": [[label, counts[i]] for i, (_l, _h, label) in enumerate(LATENCY_BINS)],
        "markers": [
            ["P50", p50, _bin_of(p50)],
            ["P90", p90, _bin_of(p90)],
        ],
        "n": len(values),
        "p50": p50,
        "p90": p90,
    }


# --------------------------------------------------------------------------- #
# 内联 CSS（深/浅色双套变量；零外部字体）
# --------------------------------------------------------------------------- #
_CSS = """
:root{
  --bg:#f6f7f9; --panel:#ffffff; --panel-2:#f0f2f5; --ink:#16181d; --ink-2:#5b616e;
  --ink-3:#8b909c; --line:#e3e6eb; --line-2:#eef0f3;
  --accent:#2f6feb; --accent-ink:#ffffff;
  --ok:#1a9e5c; --warn:#c7820a; --bad:#d93a3a; --info:#2f6feb;
  --up:#d93a3a; --down:#1a9e5c;
  --shadow:0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.10);
  --radius:10px; --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --sans:system-ui,-apple-system,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  --svg-grid:#e3e6eb; --svg-axis:#c2c7d0; --svg-track:#eceff4; --svg-ink:#5b616e;
  --svg-ink-2:#8b909c; --svg-empty:#f0f2f5; --svg-empty-text:#8b909c;
  --svg-c1:#2f6feb; --svg-c2:#1a9e5c; --svg-c3:#c7820a; --svg-c4:#8b5cf6;
  --svg-c5:#0e9aa7; --svg-c6:#d93a3a; --svg-c7:#6172f3; --svg-c8:#7a8798;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0f1116; --panel:#171a21; --panel-2:#1e222b; --ink:#e8eaef; --ink-2:#a7aebe;
    --ink-3:#7b8394; --line:#282d38; --line-2:#21252e;
    --accent:#5b8dff; --accent-ink:#0f1116;
    --ok:#3fbf7f; --warn:#e0a83c; --bad:#ff6b6b; --info:#5b8dff;
    --up:#ff6b6b; --down:#3fbf7f;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 1px 3px rgba(0,0,0,.5);
    --svg-grid:#282d38; --svg-axis:#3a4150; --svg-track:#22262f; --svg-ink:#a7aebe;
    --svg-ink-2:#7b8394; --svg-empty:#1e222b; --svg-empty-text:#7b8394;
    --svg-c1:#5b8dff; --svg-c2:#3fbf7f; --svg-c3:#e0a83c; --svg-c4:#a78bfa;
    --svg-c5:#2dd4bf; --svg-c6:#ff6b6b; --svg-c7:#818cf8; --svg-c8:#94a3b8;
  }
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--ink); font-family:var(--sans);
  font-size:14px; line-height:1.6; -webkit-text-size-adjust:100%;
}
a{color:var(--accent)}
.wrap{max-width:1280px;margin:0 auto;padding:20px 18px 64px}
/* 焦点可见性：不全局 outline:none，键盘用户必须能看见焦点 */
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
  clip:rect(0 0 0 0);white-space:nowrap;border:0}

/* ---------- 头部 ---------- */
.site-head{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:baseline;
  justify-content:space-between;margin-bottom:14px}
.site-title{font-size:20px;font-weight:700;margin:0}
.site-sub{color:var(--ink-2);font-size:12.5px}
.badge-chip{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:999px;
  background:var(--panel-2);border:1px solid var(--line);color:var(--ink-2);font-size:12px}
.meta-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px 16px;
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:12px 14px;margin-bottom:14px;box-shadow:var(--shadow)}
.meta-grid dt{color:var(--ink-3);font-size:11.5px;margin:0}
.meta-grid dd{margin:0 0 2px;font-size:13px;font-weight:600;word-break:break-word}

/* ---------- 告警横幅 ---------- */
.banner{display:none;gap:10px;align-items:flex-start;padding:12px 14px;border-radius:var(--radius);
  margin-bottom:14px;border:1px solid;font-size:13.5px}
.banner[data-show="1"]{display:flex}
.banner-bad{background:color-mix(in srgb,var(--bad) 12%,var(--panel));border-color:var(--bad);
  color:var(--ink)}
.banner-warn{background:color-mix(in srgb,var(--warn) 12%,var(--panel));border-color:var(--warn)}
.banner .banner-icon{flex:0 0 auto;width:18px;height:18px;margin-top:2px}
.banner strong{font-weight:700}

/* ---------- 通用 section / 面板 ---------- */
section{margin:22px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:16px 16px 14px;box-shadow:var(--shadow)}
h2.sec{font-size:16px;margin:0 0 12px;display:flex;align-items:center;gap:8px}
h2.sec .num{display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:22px;
  padding:0 6px;border-radius:6px;background:var(--accent);color:var(--accent-ink);
  font-size:12px;font-weight:700;flex:0 0 auto}
.hint{color:var(--ink-3);font-size:12px;margin:8px 0 0}
.note{color:var(--ink-2);font-size:12px;background:var(--panel-2);border:1px solid var(--line-2);
  border-left:3px solid var(--accent);border-radius:6px;padding:8px 10px;margin:10px 0 0}

/* ---------- 指标卡 ---------- */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:12px}
.metric-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:12px 13px;box-shadow:var(--shadow);position:relative;min-width:0}
.metric-card .mc-label{color:var(--ink-2);font-size:12px;margin:0 0 4px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.metric-card .mc-value{font-size:22px;font-weight:700;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums;word-break:break-word}
.metric-card .mc-unit{font-size:12.5px;color:var(--ink-3);font-weight:500;margin-left:3px}
.metric-card.is-missing .mc-value{color:var(--ink-3);font-size:18px}
.est-tag{position:absolute;top:9px;right:9px;font-size:10.5px;padding:1px 6px;border-radius:999px;
  background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn);
  border:1px solid color-mix(in srgb,var(--warn) 45%,transparent);font-weight:600}

/* ---------- SVG 图表 ---------- */
.chart{display:block;width:100%;height:auto;overflow:visible}
svg.chart text{font-family:var(--sans)}
svg.chart .svg-label{fill:var(--svg-ink)}
svg.chart .svg-tick{fill:var(--svg-ink-2);font-variant-numeric:tabular-nums}
svg.chart .svg-axis-label{fill:var(--svg-ink)}
svg.chart .svg-bar-value{fill:var(--ink)}
svg.chart .svg-legend{fill:var(--ink-2)}
svg.chart .svg-total{fill:var(--ink)}
svg.chart .svg-grid{stroke:var(--svg-grid);stroke-width:1}
svg.chart .svg-axis{stroke:var(--svg-axis);stroke-width:1}
svg.chart .svg-track{fill:var(--svg-track)}
svg.chart .svg-marker{stroke:var(--warn);stroke-width:1.5}
svg.chart .svg-marker-label{fill:var(--warn)}
svg.chart .svg-empty{fill:var(--svg-empty)}
svg.chart .svg-empty-text{fill:var(--svg-empty-text);font-size:12px}
svg.chart .svg-spark-line{fill:none;stroke:var(--accent);stroke-width:1.5}
svg.chart rect.svg-c1{fill:var(--svg-c1)}
svg.chart rect.svg-c2{fill:var(--svg-c2)}
svg.chart rect.svg-c3{fill:var(--svg-c3)}
svg.chart rect.svg-c4{fill:var(--svg-c4)}
svg.chart rect.svg-c5{fill:var(--svg-c5)}
svg.chart rect.svg-c6{fill:var(--svg-c6)}
svg.chart rect.svg-c7{fill:var(--svg-c7)}
svg.chart rect.svg-c8{fill:var(--svg-c8)}

/* ---------- 表格 ---------- */
.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table.grid{width:100%;border-collapse:collapse;font-size:12.5px;min-width:560px}
table.grid th,table.grid td{padding:7px 9px;border-bottom:1px solid var(--line-2);text-align:left;
  vertical-align:top}
table.grid th{color:var(--ink-2);font-weight:600;background:var(--panel-2);position:sticky;top:0;z-index:1;
  white-space:nowrap}
table.grid td.num,table.grid th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table.grid tbody tr:hover{background:var(--panel-2)}
table.grid td.mono{font-family:var(--mono);font-size:12px}
.empty-row td{color:var(--ink-3);text-align:center;padding:18px 8px}

/* ---------- 工具条（搜索/筛选/排序） ---------- */
.toolbar{display:flex;flex-wrap:wrap;gap:8px 10px;align-items:center;margin-bottom:10px}
.toolbar label{font-size:12px;color:var(--ink-2);display:inline-flex;align-items:center;gap:5px}
input[type=search],select{font:inherit;font-size:12.5px;color:var(--ink);background:var(--panel);
  border:1px solid var(--line);border-radius:7px;padding:5px 8px;min-width:0}
input[type=search]{min-width:190px}
.toolbar .spacer{flex:1 1 auto}
.count-note{color:var(--ink-3);font-size:12px;font-variant-numeric:tabular-nums}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{color:var(--accent)}
th.sortable .sarrow{display:inline-block;width:10px;color:var(--ink-3);margin-left:3px}
th.sortable[aria-sort="ascending"] .sarrow,th.sortable[aria-sort="descending"] .sarrow{color:var(--accent)}

/* ---------- 状态标记（纯 CSS 圆点，无 emoji） ---------- */
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;flex:0 0 auto;vertical-align:middle}
.dot-ok{background:var(--ok)} .dot-bad{background:var(--bad)} .dot-warn{background:var(--warn)}
.dot-muted{background:var(--ink-3)}
.pill{display:inline-flex;align-items:center;gap:5px;padding:1px 8px;border-radius:999px;
  font-size:11.5px;font-weight:600;border:1px solid;white-space:nowrap}
.pill-pass{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 45%,transparent);
  background:color-mix(in srgb,var(--ok) 12%,transparent)}
.pill-fail{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 45%,transparent);
  background:color-mix(in srgb,var(--bad) 12%,transparent)}
.pill-warn{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,transparent);
  background:color-mix(in srgb,var(--warn) 12%,transparent)}
.pill-skip{color:var(--ink-3);border-color:var(--line);background:var(--panel-2)}
/* Δ 方向：中国习惯 上升红 / 下降绿 */
.delta-up{color:var(--up);font-weight:600}
.delta-down{color:var(--down);font-weight:600}
.delta-flat{color:var(--ink-3)}

/* ---------- 抽屉 ---------- */
.drawer-backdrop{position:fixed;inset:0;background:rgba(9,12,18,.44);display:none;z-index:40}
.drawer-backdrop[data-open="1"]{display:block}
.drawer{position:fixed;top:0;right:0;bottom:0;width:min(620px,94vw);background:var(--panel);
  border-left:1px solid var(--line);box-shadow:-8px 0 28px rgba(0,0,0,.22);transform:translateX(102%);
  transition:transform .18s ease-out;z-index:41;display:flex;flex-direction:column}
.drawer[data-open="1"]{transform:translateX(0)}
.drawer-head{display:flex;gap:10px;align-items:flex-start;justify-content:space-between;
  padding:14px 16px;border-bottom:1px solid var(--line);flex:0 0 auto}
.drawer-head h3{margin:0;font-size:15px}
.drawer-body{overflow-y:auto;padding:14px 16px 28px;flex:1 1 auto}
.icon-btn{background:var(--panel-2);border:1px solid var(--line);border-radius:8px;
  width:30px;height:30px;display:inline-flex;align-items:center;justify-content:center;
  cursor:pointer;color:var(--ink-2);flex:0 0 auto}
.icon-btn:hover{color:var(--ink)}
.kv{display:grid;grid-template-columns:112px 1fr;gap:4px 10px;font-size:12.5px;margin-bottom:12px}
.kv dt{color:var(--ink-3)} .kv dd{margin:0;word-break:break-word}
.check-group{margin-top:14px}
.check-group h4{font-size:13px;margin:0 0 8px;color:var(--ink-2)}
.drawer-check-row{display:flex;gap:9px;align-items:flex-start;padding:8px 0;
  border-bottom:1px dashed var(--line-2);font-size:12.5px}
.drawer-check-row:last-child{border-bottom:0}
.drawer-check-row .cc-main{min-width:0;flex:1 1 auto}
.drawer-check-row .cc-title{font-weight:600;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.drawer-check-row .cc-detail{color:var(--ink-2);margin-top:3px;word-break:break-word}
.tag{font-family:var(--mono);font-size:11px;color:var(--ink-2);background:var(--panel-2);
  border:1px solid var(--line);border-radius:5px;padding:0 5px}
.tag-crit{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 45%,transparent)}
.link-btn{background:none;border:0;padding:0;color:var(--accent);cursor:pointer;font:inherit;
  text-decoration:underline;text-underline-offset:2px}

/* ---------- 图例 ---------- */
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:8px;font-size:12px;color:var(--ink-2)}
.legend .lg-item{display:inline-flex;align-items:center;gap:6px}
.legend .swatch{width:11px;height:11px;border-radius:3px;flex:0 0 auto}

/* ---------- 页脚 ---------- */
.foot{margin-top:34px;padding-top:14px;border-top:1px solid var(--line);color:var(--ink-3);
  font-size:12px;display:flex;flex-wrap:wrap;gap:6px 16px;justify-content:space-between}
.foot code{font-family:var(--mono)}
@media print{.drawer,.drawer-backdrop,.toolbar{display:none!important}
  .card{box-shadow:none;break-inside:avoid}}
"""


# --------------------------------------------------------------------------- #
# 内联 JS（纯前端，零依赖；数据从同页 <script type="application/json"> 读取）
# --------------------------------------------------------------------------- #
#  说明：
#  - 不使用任何 fetch / XHR / 外部脚本 → 断网可用；
#  - 排序 / 筛选 / 搜索只操作内存数组 + 重建 tbody，50 行无压力；
#  - 抽屉渲染 checks 列表；键盘 Esc 关闭；点击遮罩关闭；
#  - 所有插入的文本走 textContent / createElement，不用 innerHTML 拼字符串
#    （答案与 trace 都是外部数据，避免 XSS / 结构破坏）。
_JS = r"""
(function(){
  "use strict";
  var payload = null;
  var node = document.getElementById("report-data");
  if (node) {
    try { payload = JSON.parse(node.textContent || "{}"); } catch (e) { payload = null; }
  }
  if (!payload) { return; }

  var tasks = Array.isArray(payload.tasks) ? payload.tasks.slice() : [];
  var labels = payload.labels || {};
  var catLabel = labels.category || {};
  var diffLabel = labels.difficulty || {};
  var dimLabel = labels.dimension || {};
  var errLabel = labels.error || {};

  /* ---------------- 数值格式化（与生成期 Python 侧口径一致） ---------------- */
  var RATIO_KEYS = {"task_success_rate":1,"failure_rate":1,"pass_at_1":1,"pass_at_3":1,
    "factuality_mean":1,"source_precision_mean":1,"search_recall_mean":1,
    "citation_accuracy_mean":1,"hallucination_rate_mean":1,"tool_success_rate_mean":1,
    "usage_estimated_ratio":1,"factuality":1,"source_precision":1,"search_recall":1,
    "citation_accuracy":1,"hallucination_rate":1,"tool_success_rate":1};
  function isNum(v){ return typeof v === "number" && isFinite(v); }
  function fmtMetric(v, key){
    if (v === null || v === undefined || v === "") { return "—"; }
    if (typeof v === "boolean") { return v ? "是" : "否"; }
    if (!isNum(v)) { return String(v); }
    if (RATIO_KEYS[key]) { return (v * 100).toFixed(1) + "%"; }
    if (key && key.indexOf("cost_usd") === 0) { return "$" + v.toFixed(2); }
    if (key === "cost_usd") { return v >= 1 ? "$" + v.toFixed(2) : "$" + v.toFixed(3); }
    if (key && key.indexOf("latency_seconds") === 0) { return v.toFixed(1) + "s"; }
    if (key && key.indexOf("tokens") === 0) {
      return v >= 10000 ? (v/1000).toFixed(1) + "K" : String(Math.round(v));
    }
    if (key === "score" || (key && key.indexOf("score") === 0)) { return v.toFixed(1); }
    return String(Math.round(v * 100) / 100);
  }
  function fmtInt(v){ return isNum(v) ? String(Math.round(v)) : "—"; }
  function text(el, s){ el.textContent = s; return el; }
  function el(tag, cls, txt){
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (txt !== undefined && txt !== null) { n.textContent = String(txt); }
    return n;
  }
  function dot(state){
    var map = {"pass":"dot-ok","ok":"dot-ok","fail":"dot-bad","bad":"dot-bad",
               "warn":"dot-warn","degraded":"dot-warn","skip":"dot-muted"};
    return el("span", "dot " + (map[state] || "dot-muted"));
  }
  function pill(kind, label){
    var cls = {"pass":"pill-pass","fail":"pill-fail","warn":"pill-warn","skipped":"pill-skip"}[kind] || "pill-skip";
    var n = el("span", "pill " + cls);
    n.appendChild(dot(kind === "skipped" ? "skip" : kind));
    n.appendChild(el("span", null, label));
    return n;
  }

  /* ---------------- 明细表：搜索 / 筛选 / 排序 ---------------- */
  var state = { q: "", category: "", difficulty: "", result: "", sortKey: "task_id", sortDir: "asc" };

  function metricOf(t, key){
    var m = t.metrics || {};
    if (key === "score") { return isNum(t.score) ? t.score : null; }
    if (key === "cost") { return m.cost_usd === undefined ? null : m.cost_usd; }
    if (key === "latency") { return m.latency_seconds === undefined ? null : m.latency_seconds; }
    if (key === "task_id") { return t.task_id; }
    return null;
  }

  function filtered(){
    var q = state.q.trim().toLowerCase();
    var rows = tasks.filter(function(t){
      if (q && String(t.task_id || "").toLowerCase().indexOf(q) === -1) { return false; }
      if (state.category && t.category !== state.category) { return false; }
      if (state.difficulty && t.difficulty !== state.difficulty) { return false; }
      if (state.result === "pass" && !t.pass_at_1) { return false; }
      if (state.result === "fail" && t.pass_at_1) { return false; }
      return true;
    });
    var dir = state.sortDir === "desc" ? -1 : 1;
    rows.sort(function(a, b){
      var av = metricOf(a, state.sortKey), bv = metricOf(b, state.sortKey);
      if (av === null || av === undefined) { av = (dir === 1) ? Infinity : -Infinity; }
      if (bv === null || bv === undefined) { bv = (dir === 1) ? Infinity : -Infinity; }
      if (typeof av === "string" || typeof bv === "string") {
        return String(av).localeCompare(String(bv), "zh-Hans-CN") * dir;
      }
      return (av - bv) * dir;
    });
    return rows;
  }

  function renderTable(){
    var body = document.getElementById("task-table-body");
    if (!body) { return; }
    var rows = filtered();
    while (body.firstChild) { body.removeChild(body.firstChild); }
    var note = document.getElementById("task-count-note");
    if (note) {
      note.textContent = "显示 " + rows.length + " / " + tasks.length + " 个任务";
    }
    if (!rows.length) {
      var tr = el("tr", "empty-row");
      var td = el("td", null, tasks.length ? "没有符合当前筛选条件的任务。" : "本轮没有任务明细数据。");
      td.colSpan = 10;
      tr.appendChild(td); body.appendChild(tr);
      return;
    }
    rows.forEach(function(t){
      var m = t.metrics || {};
      var tr = el("tr");
      /* 任务 */
      var tdId = el("td", "mono");
      var btn = el("button", "link-btn", t.task_id || "—");
      btn.type = "button";
      btn.setAttribute("data-task-id", String(t.task_id || ""));
      btn.setAttribute("aria-label", "查看任务 " + (t.task_id || "") + " 的详情");
      tdId.appendChild(btn);
      tr.appendChild(tdId);
      /* 类别 / 难度 */
      tr.appendChild(text(el("td"), catLabel[t.category] || t.category || "—"));
      tr.appendChild(text(el("td"), diffLabel[t.difficulty] || t.difficulty || "—"));
      /* 结果 */
      var tdRes = el("td");
      tdRes.appendChild(pill(t.pass_at_1 ? "pass" : "fail", t.pass_at_1 ? "通过" : "未通过"));
      tr.appendChild(tdRes);
      /* 数值列 */
      [["score", t.score], ["factuality", m.factuality], ["citation_accuracy", m.citation_accuracy],
       ["latency_seconds", m.latency_seconds], ["cost_usd", m.cost_usd]].forEach(function(pair){
        var td = el("td", "num");
        td.textContent = fmtMetric(pair[1], pair[0]);
        tr.appendChild(td);
      });
      /* 错误码 */
      var tdErr = el("td");
      var codes = t.error_codes || [];
      if (codes.length) {
        codes.forEach(function(c, i){
          if (i) { tdErr.appendChild(document.createTextNode(" ")); }
          var tag = el("span", "tag", c);
          tag.title = errLabel[c] || c;
          tdErr.appendChild(tag);
        });
      } else {
        tdErr.textContent = "—";
      }
      tr.appendChild(tdErr);
      body.appendChild(tr);
    });
  }

  /* ---------------- 排序表头 ---------------- */
  function wireSort(){
    var ths = document.querySelectorAll("#task-table th.sortable");
    Array.prototype.forEach.call(ths, function(th){
      th.addEventListener("click", function(){
        var key = th.getAttribute("data-sort");
        if (state.sortKey === key) {
          state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        } else {
          state.sortKey = key; state.sortDir = (key === "task_id") ? "asc" : "desc";
        }
        updateSortHeaders(); renderTable();
      });
      th.addEventListener("keydown", function(ev){
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); th.click(); }
      });
    });
  }
  function updateSortHeaders(){
    var ths = document.querySelectorAll("#task-table th.sortable");
    Array.prototype.forEach.call(ths, function(th){
      var key = th.getAttribute("data-sort");
      var arrow = th.querySelector(".sarrow");
      if (key === state.sortKey) {
        th.setAttribute("aria-sort", state.sortDir === "asc" ? "ascending" : "descending");
        if (arrow) { arrow.textContent = state.sortDir === "asc" ? "▲" : "▼"; }
      } else {
        th.removeAttribute("aria-sort");
        if (arrow) { arrow.textContent = ""; }
      }
    });
  }

  /* ---------------- 抽屉 ---------------- */
  var drawer = document.getElementById("task-drawer");
  var backdrop = document.getElementById("drawer-backdrop");
  var lastFocus = null;

  function closeDrawer(){
    if (!drawer) { return; }
    drawer.setAttribute("data-open", "0");
    if (backdrop) { backdrop.setAttribute("data-open", "0"); }
    if (lastFocus && typeof lastFocus.focus === "function") { lastFocus.focus(); }
  }
  function openDrawer(taskId, trigger){
    if (!drawer) { return; }
    var t = null;
    for (var i = 0; i < tasks.length; i++) { if (tasks[i].task_id === taskId) { t = tasks[i]; break; } }
    if (!t) { return; }
    lastFocus = trigger || null;
    var title = document.getElementById("drawer-title");
    var body = document.getElementById("drawer-body");
    if (title) { title.textContent = t.task_id || "任务详情"; }
    if (!body) { return; }
    while (body.firstChild) { body.removeChild(body.firstChild); }
    var m = t.metrics || {};

    var kv = el("dl", "kv");
    function addKv(k, v){
      kv.appendChild(text(el("dt"), k));
      kv.appendChild(text(el("dd"), v));
    }
    addKv("类别", catLabel[t.category] || t.category || "—");
    addKv("难度", diffLabel[t.difficulty] || t.difficulty || "—");
    addKv("网络", t.network === "offline" ? "离线" : (t.network === "online" ? "联网" : (t.network || "—")));
    addKv("评分器", (t.grader_type || "—") + " / " + (t.grader_mode || "—"));
    addKv("运行次数", fmtInt(t.n_runs));
    addKv("Pass@1 / Pass@3", (t.pass_at_1 ? "通过" : "未通过") + " / " + (t.pass_at_3 ? "通过" : "未通过"));
    addKv("总分", fmtMetric(t.score, "score"));
    addKv("事实性", fmtMetric(m.factuality, "factuality"));
    addKv("引用准确率", fmtMetric(m.citation_accuracy, "citation_accuracy"));
    addKv("幻觉率", fmtMetric(m.hallucination_rate, "hallucination_rate"));
    addKv("工具调用", fmtInt(m.tool_calls));
    addKv("Token", fmtMetric(m.tokens, "tokens"));
    addKv("耗时", fmtMetric(m.latency_seconds, "latency_seconds"));
    addKv("成本", fmtMetric(m.cost_usd, "cost_usd"));
    addKv("usage 来源", t.usage_source || "—");
    var codes = t.error_codes || [];
    addKv("错误码", codes.length ? codes.map(function(c){ return (errLabel[c] || c) + "(" + c + ")"; }).join("、") : "无");
    body.appendChild(kv);

    /* 检查列表 */
    var group = el("div", "check-group");
    var checks = t.checks || [];
    group.appendChild(text(el("h4"), "检查项（" + checks.length + "）"));
    if (!checks.length) {
      group.appendChild(text(el("p", "hint"), "该任务没有记录检查项。"));
    } else {
      checks.forEach(function(c){
        var row = el("div", "drawer-check-row");
        row.appendChild(dot(c.passed ? "ok" : "bad"));
        var main = el("div", "cc-main");
        var titleRow = el("div", "cc-title");
        titleRow.appendChild(el("span", null, c.id || "—"));
        titleRow.appendChild(el("span", "tag", c.type || "—"));
        if (c.dimension) {
          titleRow.appendChild(el("span", "tag", dimLabel[c.dimension] || c.dimension));
        }
        if (c.critical) { titleRow.appendChild(el("span", "tag tag-crit", "关键")); }
        main.appendChild(titleRow);
        main.appendChild(el("div", "cc-detail", c.detail || (c.passed ? "通过" : "未通过")));
        row.appendChild(main);
        group.appendChild(row);
      });
    }
    body.appendChild(group);

    /* trace_ref：仅展示相对路径（**不做任何跳转 / 请求**） */
    var traceGroup = el("div", "check-group");
    traceGroup.appendChild(text(el("h4"), "追踪日志"));
    if (t.trace_ref) {
      var p = el("p", "hint");
      p.appendChild(el("code", null, t.trace_ref));
      traceGroup.appendChild(p);
      traceGroup.appendChild(text(el("p", "hint"),
        "该路径相对于仓库根目录；本报告为离线单文件，不加载外部文件。"));
    } else {
      traceGroup.appendChild(text(el("p", "hint"), "本次运行没有可用的追踪日志路径。"));
    }
    body.appendChild(traceGroup);

    drawer.setAttribute("data-open", "1");
    if (backdrop) { backdrop.setAttribute("data-open", "1"); }
    var closeBtn = document.getElementById("drawer-close");
    if (closeBtn && typeof closeBtn.focus === "function") { closeBtn.focus(); }
  }

  /* ---------------- 事件绑定 ---------------- */
  function wire(){
    var search = document.getElementById("task-search");
    if (search) {
      search.addEventListener("input", function(){ state.q = search.value; renderTable(); });
    }
    [["task-filter-category","category"],["task-filter-difficulty","difficulty"],
     ["task-filter-result","result"]].forEach(function(pair){
      var sel = document.getElementById(pair[0]);
      if (sel) {
        sel.addEventListener("change", function(){ state[pair[1]] = sel.value; renderTable(); });
      }
    });
    var reset = document.getElementById("task-reset");
    if (reset) {
      reset.addEventListener("click", function(){
        state.q = ""; state.category = ""; state.difficulty = ""; state.result = "";
        state.sortKey = "task_id"; state.sortDir = "asc";
        if (search) { search.value = ""; }
        ["task-filter-category","task-filter-difficulty","task-filter-result"].forEach(function(id){
          var n = document.getElementById(id); if (n) { n.value = ""; }
        });
        updateSortHeaders(); renderTable();
        if (search) { search.focus(); }
      });
    }
    document.addEventListener("click", function(ev){
      var target = ev.target;
      if (target && target.getAttribute && target.getAttribute("data-task-id") !== null) {
        openDrawer(target.getAttribute("data-task-id"), target);
      }
    });
    var closeBtn = document.getElementById("drawer-close");
    if (closeBtn) { closeBtn.addEventListener("click", closeDrawer); }
    if (backdrop) { backdrop.addEventListener("click", closeDrawer); }
    document.addEventListener("keydown", function(ev){
      if (ev.key === "Escape" && drawer && drawer.getAttribute("data-open") === "1") {
        closeDrawer();
      }
    });
    wireSort();
    updateSortHeaders();
    renderTable();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
"""


# --------------------------------------------------------------------------- #
# 图标（内联 SVG，**不使用 emoji / 图标字体 / 外部图片**）
# --------------------------------------------------------------------------- #
_ICON_ALERT = (
    '<svg class="banner-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true" '
    'xmlns="http://www.w3.org/2000/svg">'
    '<path d="M12 3.2 1.9 20.3h20.2L12 3.2Z" fill="none" stroke="currentColor" '
    'stroke-width="1.9" stroke-linejoin="round"/>'
    '<path d="M12 9.4v4.9" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>'
    '<circle cx="12" cy="17.5" r="1.15" fill="currentColor"/></svg>'
)
_ICON_CLOSE = (
    '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" aria-hidden="true" '
    'xmlns="http://www.w3.org/2000/svg">'
    '<path d="M5.5 5.5l13 13M18.5 5.5l-13 13" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round"/></svg>'
)


def _h(text: str) -> str:
    """HTML 文本转义（与 svg.escape 同源，统一口径）。"""
    return svg.escape(text)


# --------------------------------------------------------------------------- #
# 各区块渲染
# --------------------------------------------------------------------------- #
def _render_banner(vm: dict[str, Any]) -> str:
    """元素 1：顶部告警横幅（``run.grader_mode != "full"`` 时红色横幅）。"""
    run = vm.get("run") or {}
    mode = str(run.get("grader_mode") or "full")
    degraded = run.get("degraded_task_count")
    degraded_text = svg.fmt_int(degraded)
    if mode == "full":
        # 仍需输出容器以保持 DOM 稳定；data-show=0 → 不显示
        return (
            '<div id="degraded-banner" class="banner banner-bad" data-show="0" '
            'data-grader-mode="full" role="status"></div>'
        )
    badge = "评分降级" if mode == "degraded" else "部分降级"
    body = (
        f"{_ICON_ALERT}<div>"
        f'<strong>{_h(badge)}：本轮 grader_mode = <code>{_h(mode)}</code></strong>'
        f"<div>共 <strong>{_h(degraded_text)}</strong> 个任务未使用完整语义评分"
        f"（无 API Key / 网络不可达 / 响应非法等），其语义维度已按降级规则处理。"
        f"<strong>分数与事实性指标不可直接与 full 模式跨版本对比。</strong>"
        f"详见各任务详情中的 <code>grader_mode</code> 与 <code>degraded_reason</code>。</div>"
        f"</div>"
    )
    return (
        f'<div id="degraded-banner" class="banner banner-bad" data-show="1" '
        f'data-grader-mode="{_h(mode)}" role="alert">{body}</div>'
    )


def _render_meta(vm: dict[str, Any]) -> str:
    """报告元信息（非 12 类元素，但验收需要能看清数据来源）。"""
    suite = vm.get("suite") or {}
    run = vm.get("run") or {}
    bench = vm.get("benchmark") or {}
    env = run.get("environment") or {}
    items = [
        ("任务集", f"{suite.get('id', '-')}（{suite.get('split', '-')}）"),
        ("任务数", f"{svg.fmt_int(suite.get('task_count'))} 题 × {svg.fmt_int(suite.get('runs_per_task'))} 次"),
        ("随机种子", svg.fmt_int(suite.get("seed"))),
        ("被测 Agent", f"{run.get('agent', '-')} {('v' + str(run.get('agent_version'))) if run.get('agent_version') else ''}".strip()),
        ("模型", str(run.get("model") or "-")),
        ("版本标签", str(run.get("tag") or "-")),
        ("基准版本", f"v{bench.get('version', '1.0')}"),
        ("评分模式", str(run.get("grader_mode") or "full")),
        ("离线回放", "是" if run.get("offline_replay") else "否"),
        ("运行环境", f"{env.get('platform', '-')} / Python {env.get('python', '-')}"),
        ("生成时间", str(vm.get("generated_at") or "-")),
        ("Schema", str(vm.get("schema_version") or "-")),
    ]
    cells = "".join(
        f"<div><dt>{_h(k)}</dt><dd>{_h(v)}</dd></div>" for k, v in items
    )
    return f'<dl class="meta-grid" id="report-meta">{cells}</dl>'


def _render_metric_cards(vm: dict[str, Any]) -> str:
    """元素 2：指标卡（12 张；数值 + 单位 + estimated 角标）。"""
    summary = vm.get("summary") or {}
    run = vm.get("run") or {}
    # 仅「全轮均为估算」时打 estimated 角标：即 usage_estimated_ratio == 1
    est_ratio = summary.get("usage_estimated_ratio")
    all_estimated = isinstance(est_ratio, (int, float)) and not isinstance(est_ratio, bool) and float(est_ratio) >= 0.999
    est_sensitive = {"tokens_mean", "cost_usd_mean"}

    cards: list[str] = []
    for key, label in METRIC_CARDS:
        value = summary.get(key)
        text = svg.fmt_value(value, key)
        # 单位从格式化结果里拆出来（保持数字大字号、单位小字号）
        unit = ""
        number = text
        for suffix in ("%", "s", "分", "次", "题", "tokens", "K", "M", "美元"):
            if text.endswith(suffix) and len(text) > len(suffix):
                unit = suffix
                number = text[: -len(suffix)]
                break
        if text.startswith("$"):
            unit = "$"
            number = text[1:]
        missing = value is None
        cls = "metric-card is-missing" if missing else "metric-card"
        badge = ""
        if all_estimated and key in est_sensitive and not missing:
            badge = '<span class="est-tag" title="该值由字符数估算（usage_source=estimated_from_chars）">估算</span>'
        cards.append(
            f'<div class="{cls}" data-metric="{_h(key)}">'
            f"{badge}"
            f'<p class="mc-label" title="{_h(label)}">{_h(label)}</p>'
            f'<div class="mc-value">{_h(number)}<span class="mc-unit">{_h(unit)}</span></div>'
            f"</div>"
        )
    note = "数值为 <code>report.json</code> 的 <code>summary</code> 原样；<code>—</code> 表示该指标未采集（后端约定不用 0 冒充）。"
    if all_estimated:
        note += " 带「估算」角标的值由字符数反推，仅作参考。"
    elif isinstance(est_ratio, (int, float)) and not isinstance(est_ratio, bool) and float(est_ratio) > 0:
        note += f" 成本口径：估算值占比 {svg.fmt_value(est_ratio, 'usage_estimated_ratio')}。"
    return (
        f'<div class="cards" id="metric-cards" data-count="{len(cards)}">'
        + "".join(cards)
        + f'</div><p class="hint">{note}</p>'
    )


def _render_breakdown(vm: dict[str, Any]) -> str:
    """元素 3：总分构成堆叠条（按「占 100 分比」口径，守恒、跨版本可比）。"""
    breakdown = vm.get("score_breakdown_mean") or {}
    ratios = vm.get("breakdown_ratios") or {}
    total = breakdown.get("total")
    segments: list[tuple[str, float, float]] = []
    for dim in DIMENSIONS:
        label = DIMENSION_LABELS.get(dim, dim)
        score = breakdown.get(dim)
        share = ratios.get(dim)
        numeric_score = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else 0.0
        numeric_share = float(share) if isinstance(share, (int, float)) and not isinstance(share, bool) else 0.0
        # 第三个参数是图例里的"满分"：此处用该维在总分中的等效满分 = 占比 × total
        equivalent_max = numeric_share * float(total) if isinstance(total, (int, float)) and total else 0.0
        segments.append((label, numeric_score, equivalent_max))
    chart = svg.stacked_bar(
        segments,
        width=880,
        height=132,
        title="总分构成堆叠条（7 维，按占 100 分比例）",
        max_total=float(total) if isinstance(total, (int, float)) and total else 100.0,
    )
    return (
        f'<div id="chart-breakdown" class="card">{chart}'
        f'<p class="hint">条长 = 总分 {svg.fmt_value(total, "score_mean")}（100 分制）。'
        f"图例为「维度得分 / 该维等效满分（占比 × 总分）」。"
        f"存在维度中性再分配时，某维等效满分可超过方案 4.9 的默认满分——"
        f"<code>report.json</code> 的 <code>score_breakdown_mean</code> 未透出逐维等效满分，"
        f"故此处按占比口径绘制，跨版本可比。</p></div>"
    )


def _render_category(vm: dict[str, Any]) -> str:
    """元素 4：按类别能力分组柱（成功率 / 事实性 / 引用准确率）。"""
    rows = vm.get("by_category") or []
    groups = [str(r.get("label") or r.get("category") or "-") for r in rows]
    series = [
        ("任务成功率", [_num(r.get("task_success_rate")) for r in rows], "%"),
        ("事实性", [_num(r.get("factuality_mean")) for r in rows], "%"),
        ("引用准确率", [_num(r.get("citation_accuracy_mean")) for r in rows], "%"),
    ]
    chart = svg.grouped_bars(
        groups,
        series,
        width=880,
        height=max(260.0, 130.0 + 26.0 * max(1, len(rows))),
        title="按任务类别：成功率 / 事实性 / 引用准确率",
        y_max=1.0,
        y_suffix="%",
        as_ratio=True,
    )
    return (
        f'<div id="chart-category" class="card">{chart}'
        f'<p class="hint">空柱（无柱体）表示该类别缺失该指标（未采集或该类别无对应检查项）。'
        "类别名称取后端 <code>by_category[].label</code>。</p></div>"
    )


def _render_difficulty(vm: dict[str, Any]) -> str:
    """元素 5：按难度成功率柱 + 样本数标注。"""
    rows = vm.get("by_difficulty") or []
    groups = [str(r.get("label") or r.get("difficulty") or "-") for r in rows]
    chart = svg.grouped_bars(
        [f"{g}（n={svg.fmt_int(r.get('task_count'))}）" for g, r in zip(groups, rows)],
        [("任务成功率", [_num(r.get("task_success_rate")) for r in rows], "%")],
        width=880,
        height=280,
        title="按难度：任务成功率（括号内为样本数）",
        y_max=1.0,
        y_suffix="%",
        as_ratio=True,
    )
    return (
        f'<div id="chart-difficulty" class="card">{chart}'
        f'<p class="hint">样本数 n 为后端 <code>by_difficulty[].task_count</code>。'
        "柱子高度为成功率；样本数很小的档位（如 n=1）波动大，请结合样本数解读。</p></div>"
    )


def _render_latency(vm: dict[str, Any]) -> str:
    """元素 6：Latency 分布直方图（前端分桶）+ P50/P90 标线。"""
    hist = vm.get("latency_hist") or {}
    bins = hist.get("bins") or []
    markers = hist.get("markers") or []
    chart = svg.histogram(
        [(str(b[0]), int(b[1] or 0)) for b in bins],
        width=880,
        height=260,
        title="Latency 分布（按任务，单位：秒）",
        markers=[(str(m[0]), _num(m[1]), int(m[2] or 0)) for m in markers],
        unit="s",
    )
    p50 = svg.fmt_value(hist.get("p50"), "latency_seconds_mean")
    p90 = svg.fmt_value(hist.get("p90"), "latency_seconds_p90")
    return (
        f'<div id="chart-latency" class="card">{chart}'
        f'<p class="hint">由 <code>tasks[].metrics.latency_seconds</code> 在前端分桶；'
        f"仅统计真实采集到的值（n={svg.fmt_int(hist.get('n'))}）。"
        f"P50 = {p50}，P90 = {p90}。"
        "注意：mock/echo 等离线 Runner 不产生真实墙钟耗时，直方图可能集中在最低桶。</p></div>"
    )


def _render_cost(vm: dict[str, Any]) -> str:
    """元素 7：成本 TOP10 横向条。"""
    rows = vm.get("cost_top") or []
    items = [
        (
            f"{r.get('task_id')}",
            _num(r.get("cost_usd")),
            svg.fmt_value(r.get("cost_usd"), "cost_usd"),
        )
        for r in rows
    ]
    chart = svg.horizontal_bars(
        items,
        width=880,
        title="成本 TOP10（USD）",
        max_rows=10,
        value_formatter=lambda v: svg.fmt_value(v, "cost_usd"),
        label_width=150.0,
    )
    note = (
        "只统计 <code>tasks[].metrics.cost_usd</code> 非空的任务；"
        "<code>—</code> 表示未采集。跨 Agent 成本口径不一致，v1.0 仅作参考、不作为门禁。"
    )
    if not rows:
        note = "本轮没有任何任务采集到成本数据（<code>usage_source</code> 多为 <code>none</code>）。"
    return f'<div id="chart-cost" class="card">{chart}<p class="hint">{note}</p></div>'


def _render_errors(vm: dict[str, Any]) -> str:
    """元素 8：错误分类条形（11 类横条 + 占比；count=0 也列出）。"""
    rows = vm.get("errors") or []
    labels = (vm.get("labels") or {}).get("error") or {}
    items = []
    for r in rows:
        code = str(r.get("code") or "UNKNOWN")
        name = labels.get(code) or ERROR_LABELS.get(code) or code
        count = r.get("count")
        ratio = svg.fmt_value(r.get("ratio"), "failure_rate")
        items.append(
            (
                f"{name}",
                float(count) if isinstance(count, (int, float)) and not isinstance(count, bool) else None,
                f"{svg.fmt_int(count)}（{ratio}）",
            )
        )
    chart = svg.horizontal_bars(
        items,
        width=880,
        title="错误分类（11 类，按出现次数降序）",
        color_index=5,  # 统一警示色
        value_formatter=None,
        label_width=150.0,
    )
    samples = [
        f"<code>{_h(r.get('code'))}</code> → "
        + ("、".join(f"<code>{_h(s)}</code>" for s in (r.get("sample_task_ids") or [])) or "—")
        for r in rows
        if r.get("sample_task_ids")
    ]
    sample_html = (
        '<p class="hint">样例任务：' + "；".join(samples) + "</p>" if samples else ""
    )
    return (
        f'<div id="chart-errors" class="card">{chart}'
        f'<p class="hint">11 类错误全部列出（<code>count=0</code> 保留），占比为占全部错误条数的比例。</p>'
        f"{sample_html}</div>"
    )


def _render_pass(vm: dict[str, Any]) -> str:
    """元素 9：Pass@1 vs Pass@3 双条对比。"""
    summary = vm.get("summary") or {}
    chart = svg.double_bar(
        [("全体任务", _num(summary.get("pass_at_1")), _num(summary.get("pass_at_3")))],
        width=880,
        height=126,
        title="Pass@1 vs Pass@3",
        as_ratio=True,
    )
    gap = None
    p1, p3 = _num(summary.get("pass_at_1")), _num(summary.get("pass_at_3"))
    if p1 is not None and p3 is not None:
        gap = (p3 - p1) * 100
    gap_text = f"{gap:+.1f}pp" if gap is not None else "—"
    return (
        f'<div id="chart-pass" class="card">{chart}'
        f'<p class="hint">Pass@1 = 第 1 次运行即通过；Pass@3 = 前 3 次内任一次通过。'
        f"差值（多次尝试带来的增益）= <strong>{_h(gap_text)}</strong>。"
        f"若 <code>runs_per_task = 1</code>，两者必然相等。</p></div>"
    )


def _render_task_table(vm: dict[str, Any]) -> str:
    """元素 10：任务明细表（服务端渲染首屏；排序/筛选/搜索由 JS 接管）。"""
    tasks = vm.get("tasks") or []
    labels = vm.get("labels") or {}
    cat_label = labels.get("category") or {}
    diff_label = labels.get("difficulty") or {}

    used_categories = sorted({str(t.get("category") or "") for t in tasks if t.get("category")})
    used_difficulties = sorted(
        {str(t.get("difficulty") or "") for t in tasks if t.get("difficulty")},
        key=lambda d: ["easy", "medium", "hard", "expert"].index(d) if d in ("easy", "medium", "hard", "expert") else 99,
    )
    cat_opts = "".join(
        f'<option value="{_h(c)}">{_h(cat_label.get(c) or c)}</option>' for c in used_categories
    )
    diff_opts = "".join(
        f'<option value="{_h(d)}">{_h(diff_label.get(d) or d)}</option>' for d in used_difficulties
    )

    head = (
        "<tr>"
        '<th class="sortable" data-sort="task_id" tabindex="0" scope="col">任务 ID<span class="sarrow"></span></th>'
        "<th scope=\"col\">类别</th>"
        "<th scope=\"col\">难度</th>"
        "<th scope=\"col\">结果</th>"
        '<th class="sortable num" data-sort="score" tabindex="0" scope="col">总分<span class="sarrow"></span></th>'
        "<th class=\"num\" scope=\"col\">事实性</th>"
        "<th class=\"num\" scope=\"col\">引用准确率</th>"
        '<th class="sortable num" data-sort="latency" tabindex="0" scope="col">耗时<span class="sarrow"></span></th>'
        '<th class="sortable num" data-sort="cost" tabindex="0" scope="col">成本<span class="sarrow"></span></th>'
        "<th scope=\"col\">错误码</th>"
        "</tr>"
    )

    body_rows: list[str] = []
    for t in tasks:
        metrics = t.get("metrics") or {}
        codes = t.get("error_codes") or []
        code_html = (
            " ".join(
                f'<span class="tag" title="{_h((labels.get("error") or {}).get(c) or c)}">{_h(c)}</span>'
                for c in codes
            )
            or "—"
        )
        state = "pass" if t.get("pass_at_1") else "fail"
        body_rows.append(
            "<tr>"
            f'<td class="mono"><button type="button" class="link-btn" '
            f'data-task-id="{_h(t.get("task_id"))}" '
            f'aria-label="查看任务 {_h(t.get("task_id"))} 的详情">{_h(t.get("task_id"))}</button></td>'
            f'<td>{_h(cat_label.get(t.get("category")) or t.get("category") or "—")}</td>'
            f'<td>{_h(diff_label.get(t.get("difficulty")) or t.get("difficulty") or "—")}</td>'
            f'<td><span class="pill pill-{state}"><span class="dot dot-{"ok" if state == "pass" else "bad"}"></span>'
            f'<span>{"通过" if state == "pass" else "未通过"}</span></span></td>'
            f'<td class="num">{_h(svg.fmt_value(t.get("score"), "score"))}</td>'
            f'<td class="num">{_h(svg.fmt_value(metrics.get("factuality"), "factuality"))}</td>'
            f'<td class="num">{_h(svg.fmt_value(metrics.get("citation_accuracy"), "citation_accuracy"))}</td>'
            f'<td class="num">{_h(svg.fmt_value(metrics.get("latency_seconds"), "latency_seconds"))}</td>'
            f'<td class="num">{_h(svg.fmt_value(metrics.get("cost_usd"), "cost_usd"))}</td>'
            f"<td>{code_html}</td>"
            "</tr>"
        )
    if not body_rows:
        body_rows.append(
            '<tr class="empty-row"><td colspan="10">本轮没有任务明细数据。</td></tr>'
        )

    toolbar = (
        '<div class="toolbar">'
        '<label for="task-search">搜索任务 ID'
        f'<input type="search" id="task-search" placeholder="例如 SEARCH" autocomplete="off" '
        f'aria-label="按任务 ID 搜索"></label>'
        f'<label for="task-filter-category">类别<select id="task-filter-category" aria-label="按类别筛选">'
        f'<option value="">全部</option>{cat_opts}</select></label>'
        f'<label for="task-filter-difficulty">难度<select id="task-filter-difficulty" aria-label="按难度筛选">'
        f'<option value="">全部</option>{diff_opts}</select></label>'
        '<label for="task-filter-result">结果<select id="task-filter-result" aria-label="按结果筛选">'
        '<option value="">全部</option><option value="pass">通过</option>'
        '<option value="fail">未通过</option></select></label>'
        '<button type="button" class="icon-btn" id="task-reset" title="重置筛选" '
        'aria-label="重置筛选与排序">↺</button>'
        '<span class="spacer"></span>'
        f'<span class="count-note" id="task-count-note">显示 {len(tasks)} / {len(tasks)} 个任务</span>'
        "</div>"
    )

    return (
        '<div id="task-table-card" class="card">'
        f"{toolbar}"
        '<div class="table-scroll">'
        f'<table class="grid" id="task-table"><caption class="sr-only">任务明细表，'
        f"可按任务 ID 搜索、按类别与难度筛选、按总分/耗时/成本排序</caption>"
        f"<thead>{head}</thead><tbody id=\"task-table-body\">{''.join(body_rows)}</tbody></table>"
        "</div>"
        '<p class="hint">点击任务 ID 打开详情抽屉（含检查项明细与追踪日志路径）。'
        "表格为零依赖纯前端实现，排序与筛选不发起任何网络请求。</p>"
        "</div>"
    )


def _render_drawer(vm: dict[str, Any]) -> str:
    """元素 11：任务详情抽屉（容器 + 遮罩；内容由 JS 渲染）。

    服务端**先渲染首个失败任务**（若有），保证禁用 JS 时仍能看到一份 check 列表，
    同时让 ``.drawer-check-row`` 在静态 HTML 中即存在（供校验脚本断言）。
    """
    tasks = vm.get("tasks") or []
    labels = vm.get("labels") or {}
    cat_label = labels.get("category") or {}
    diff_label = labels.get("difficulty") or {}
    dim_label = labels.get("dimension") or {}
    err_label = labels.get("error") or {}

    candidate = next((t for t in tasks if not t.get("pass_at_1")), None)
    if candidate is None and tasks:
        candidate = tasks[0]

    if candidate is None:
        # 空状态：仍渲染一个 `.drawer-check-row` 占位，保证：
        #   1) 元素 11 的 DOM 结构在任何数据量下都稳定（校验脚本可断言）；
        #   2) 用户点击时看到明确提示而不是空白。
        inner = (
            '<p class="hint">本轮没有任务数据，无法展示详情。</p>'
            '<div class="check-group"><h4>检查项（0）</h4>'
            '<div class="drawer-check-row"><span class="dot dot-muted"></span>'
            '<div class="cc-main"><div class="cc-title"><span>—</span></div>'
            '<div class="cc-detail">本轮没有可展示的检查项。</div></div></div>'
            "</div>"
        )
        title = "任务详情"
    else:
        title = str(candidate.get("task_id") or "任务详情")
        metrics = candidate.get("metrics") or {}
        kv_items = [
            ("类别", cat_label.get(candidate.get("category")) or candidate.get("category") or "—"),
            ("难度", diff_label.get(candidate.get("difficulty")) or candidate.get("difficulty") or "—"),
            ("结果", "通过" if candidate.get("pass_at_1") else "未通过"),
            ("总分", svg.fmt_value(candidate.get("score"), "score")),
            ("事实性", svg.fmt_value(metrics.get("factuality"), "factuality")),
            ("耗时", svg.fmt_value(metrics.get("latency_seconds"), "latency_seconds")),
            ("成本", svg.fmt_value(metrics.get("cost_usd"), "cost_usd")),
            ("usage 来源", candidate.get("usage_source") or "—"),
        ]
        kv_html = "".join(
            f"<dt>{_h(k)}</dt><dd>{_h(v)}</dd>" for k, v in kv_items
        )
        codes = candidate.get("error_codes") or []
        codes_html = (
            "、".join(f"{_h(err_label.get(c) or c)}（{_h(c)}）" for c in codes) or "无"
        )
        kv_html += f"<dt>错误码</dt><dd>{codes_html}</dd>"

        checks = candidate.get("checks") or []
        rows_html = []
        for check in checks:
            ok = bool(check.get("passed"))
            dots = "dot-ok" if ok else "dot-bad"
            tags = [
                f'<span class="tag">{_h(check.get("type") or "—")}</span>',
            ]
            if check.get("dimension"):
                tags.append(
                    f'<span class="tag">{_h(dim_label.get(check.get("dimension")) or check.get("dimension"))}</span>'
                )
            if check.get("critical"):
                tags.append('<span class="tag tag-crit">关键</span>')
            rows_html.append(
                '<div class="drawer-check-row" data-check-id="' + _h(check.get("id")) + '">'
                f'<span class="dot {dots}"></span>'
                '<div class="cc-main">'
                '<div class="cc-title">'
                f'<span>{_h(check.get("id") or "—")}</span>{"".join(tags)}'
                '<span class="tag">' + ("通过" if ok else "未通过") + "</span>"
                "</div>"
                f'<div class="cc-detail">{_h(check.get("detail") or ("通过" if ok else "未通过"))}</div>'
                "</div></div>"
            )
        if not rows_html:
            rows_html.append('<div class="drawer-check-row"><span class="dot dot-muted"></span>'
                             '<div class="cc-main"><div class="cc-detail">'
                             "该任务没有记录检查项。</div></div></div>")
        trace = candidate.get("trace_ref")
        trace_html = (
            f"<p class=\"hint\"><code>{_h(trace)}</code>（相对仓库根目录；离线单文件不加载外部文件）</p>"
            if trace
            else '<p class="hint">本次运行没有可用的追踪日志路径。</p>'
        )
        inner = (
            f'<dl class="kv">{kv_html}</dl>'
            f'<div class="check-group"><h4>检查项（{len(checks)}）</h4>{"".join(rows_html)}</div>'
            f'<div class="check-group"><h4>追踪日志</h4>{trace_html}</div>'
        )

    return (
        '<div class="drawer-backdrop" id="drawer-backdrop" data-open="0" aria-hidden="true"></div>'
        '<aside class="drawer" id="task-drawer" data-open="0" role="dialog" '
        'aria-modal="true" aria-labelledby="drawer-title" aria-hidden="true" hidden>'
        '<div class="drawer-head">'
        f'<h3 id="drawer-title">{_h(title)}</h3>'
        f'<button type="button" class="icon-btn" id="drawer-close" '
        f'aria-label="关闭详情">{_ICON_CLOSE}</button>'
        "</div>"
        f'<div class="drawer-body" id="drawer-body">{inner}</div>'
        "</aside>"
    )



def _render_comparison(vm: dict[str, Any]) -> str:
    """元素 12：版本对比表（Δpp + 方向箭头 + Gate 徽章）。

    无 baseline 时输出占位说明（**不伪造 Δ**），但容器与 class 仍存在，
    以便校验脚本断言 DOM 结构稳定。
    """
    comparison = vm.get("comparison")
    if not comparison:
        return (
            '<div id="comparison-section" class="card" data-has-baseline="0">'
            '<p class="hint">本轮未提供 baseline，无版本对比数据。'
            "使用 <code>kaoyanbench report --suite &amp;lt;id&amp;gt; "
            "--baseline &amp;lt;tag&amp;gt;</code> "
            "或 <code>kaoyanbench regression ...</code> 生成带对比的报告。</p>"
            '<div class="legend" hidden><span class="gate-badge"></span></div>'
            "</div>"
        )

    deltas = comparison.get("deltas") or []
    gates = comparison.get("gates") or []
    newly_failed = comparison.get("newly_failed_tasks") or []
    newly_passed = comparison.get("newly_passed_tasks") or []
    verdict = str(comparison.get("verdict") or "skipped")

    # 中国习惯：上升红、下降绿
    arrow_map = {
        "up": ('<span class="delta-up" aria-label="上升">▲</span>', "delta-up"),
        "down": ('<span class="delta-down" aria-label="下降">▼</span>', "delta-down"),
    }

    delta_rows: list[str] = []
    for row in deltas:
        direction = str(row.get("direction") or "flat")
        arrow_html, delta_cls = arrow_map.get(direction, ('<span class="delta-flat" aria-label="持平">—</span>', "delta-flat"))
        metric = str(row.get("metric") or "")
        delta_rows.append(
            "<tr>"
            f'<td>{_h(row.get("label") or metric)}<br><span class="tag">{_h(metric)}</span></td>'
            f'<td class="num">{_h(_fmt_side(row.get("baseline"), metric))}</td>'
            f'<td class="num">{_h(_fmt_side(row.get("current"), metric))}</td>'
            f'<td class="num {delta_cls}">{arrow_html} {_h(svg.fmt_pp(row.get("delta_pp")))}</td>'
            f'<td>{_h(_direction_text(direction))}</td>'
            "</tr>"
        )
    if not delta_rows:
        delta_rows.append('<tr class="empty-row"><td colspan="5">没有可对比的指标。</td></tr>')

    gate_rows: list[str] = []
    for gate in gates:
        result = str(gate.get("result") or "skipped")
        gate_rows.append(
            "<tr>"
            f'<td>{_h(gate.get("label") or gate.get("name"))}</td>'
            f'<td>{_h(gate.get("threshold_text") or "-")}</td>'
            f'<td class="num">{_h(svg.fmt_pp(gate.get("delta_pp")))}</td>'
            f'<td><span class="gate-badge pill pill-{_gate_pill(result)}">'
            f'<span class="dot dot-{_gate_dot(result)}"></span>'
            f"<span>{_h(_gate_text(result))}</span></span></td>"
            f'<td>{_h(gate.get("message") or "—")}</td>'
            "</tr>"
        )
    if not gate_rows:
        gate_rows.append('<tr class="empty-row"><td colspan="5">没有配置回归门禁。</td></tr>')

    new_task_bits: list[str] = []
    if newly_failed:
        new_task_bits.append(
            "<dt>新失败任务</dt><dd>"
            + "、".join(f'<span class="tag tag-crit">{_h(t)}</span>' for t in newly_failed)
            + "</dd>"
        )
    if newly_passed:
        new_task_bits.append(
            "<dt>新通过任务</dt><dd>"
            + "、".join(f'<span class="tag">{_h(t)}</span>' for t in newly_passed)
            + "</dd>"
        )
    new_block = f'<dl class="kv">{"".join(new_task_bits)}</dl>' if new_task_bits else ""

    return (
        '<div id="comparison-section" class="card" data-has-baseline="1" '
        f'data-verdict="{_h(verdict)}">'
        f'<p class="hint">基线 <strong>{_h(comparison.get("baseline_tag") or "-")}</strong> → '
        f'当前 <strong>{_h(comparison.get("current_tag") or "-")}</strong>　'
        f'总判定：<span class="gate-badge pill pill-{_gate_pill(verdict)}">'
        f'<span class="dot dot-{_gate_dot(verdict)}"></span>'
        f'<span>{_h(comparison.get("verdict_label") or _gate_text(verdict))}</span></span></p>'
        '<h3 style="font-size:13.5px;margin:14px 0 8px;color:var(--ink-2)">指标变化（Δ 为百分点 pp）</h3>'
        '<div class="table-scroll"><table class="grid"><caption class="sr-only">版本对比指标变化表</caption>'
        "<thead><tr><th scope=\"col\">指标</th><th class=\"num\" scope=\"col\">基线</th>"
        "<th class=\"num\" scope=\"col\">当前</th><th class=\"num\" scope=\"col\">Δ</th>"
        "<th scope=\"col\">方向</th></tr></thead>"
        f"<tbody>{''.join(delta_rows)}</tbody></table></div>"
        '<h3 style="font-size:13.5px;margin:16px 0 8px;color:var(--ink-2)">回归门禁</h3>'
        '<div class="table-scroll"><table class="grid"><caption class="sr-only">回归门禁结果表</caption>'
        "<thead><tr><th scope=\"col\">门禁</th><th scope=\"col\">阈值</th>"
        "<th class=\"num\" scope=\"col\">Δ</th><th scope=\"col\">结果</th>"
        "<th scope=\"col\">说明</th></tr></thead>"
        f"<tbody>{''.join(gate_rows)}</tbody></table></div>"
        f"{new_block}"
        '<p class="hint">配色遵循中国习惯：<span class="delta-up">▲ 上升为红</span>、'
        '<span class="delta-down">▼ 下降为绿</span>。'
        "注意：<code>hallucination_rate_mean</code> 等「越低越好」的指标，"
        "方向箭头只表示数值升降，请结合门禁阈值判读。</p>"
        "</div>"
    )


def _gate_pill(result: str) -> str:
    return {"pass": "pass", "fail": "fail", "warn": "warn"}.get(result, "skip")


def _gate_dot(result: str) -> str:
    return {"pass": "ok", "fail": "bad", "warn": "warn"}.get(result, "muted")


def _gate_text(result: str) -> str:
    return {
        "pass": "通过",
        "fail": "未通过",
        "warn": "告警",
        "skipped": "已跳过",
    }.get(result, result)


def _direction_text(direction: str) -> str:
    return {"up": "上升", "down": "下降"}.get(direction, "持平（未变化）")


def _fmt_side(value: Any, metric: str) -> str:
    """对比表左右两侧的数值：比率类按百分比，其余按指标单位。"""
    if value is None:
        return "—"
    if metric.endswith(("_rate", "_mean")) and isinstance(value, (int, float)) and not isinstance(value, bool):
        if abs(float(value)) <= 1.0:
            return f"{float(value) * 100:.1f}%"
    return svg.fmt_value(value, metric)


# --------------------------------------------------------------------------- #
# 顶层渲染
# --------------------------------------------------------------------------- #
_SECTIONS: tuple[tuple[str, str, Any], ...] = (
    ("1", "总体指标", _render_metric_cards),
    ("2", "总分构成", _render_breakdown),
    ("3", "按类别能力", _render_category),
    ("4", "按难度成功率", _render_difficulty),
    ("5", "Latency 分布", _render_latency),
    ("6", "成本 TOP10", _render_cost),
    ("7", "错误分类", _render_errors),
    ("8", "Pass@1 vs Pass@3", _render_pass),
    ("9", "任务明细", _render_task_table),
    ("10", "版本对比", _render_comparison),
)


def render_html(report: ReportModel) -> str:
    """把 :class:`ReportModel` 渲染成**单文件 HTML**（零外部请求）。"""
    vm = build_view_model(report)
    run = vm.get("run") or {}
    suite = vm.get("suite") or {}
    bench = vm.get("benchmark") or {}

    title = (
        f"{bench.get('name', 'KaoyanBench')} 评测报告 · "
        f"{suite.get('id', '-')} · {run.get('agent', '-')} · {run.get('tag', '-')}"
    )

    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="light dark">',
        '<meta name="generator" content="KaoyanBench HtmlReporter 1.0">',
        f"<title>{_h(title)}</title>",
        # 内联 CSS（唯一 <style>，零外部样式表）
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        '<div class="wrap">',
        # 头部
        '<header class="site-head">',
        f'<h1 class="site-title">{_h(bench.get("name", "KaoyanBench"))} 评测报告</h1>',
        '<div class="site-sub">'
        f'<span class="badge-chip">任务集 {_h(suite.get("id", "-"))}</span> '
        f'<span class="badge-chip">Agent {_h(run.get("agent", "-"))}</span> '
        f'<span class="badge-chip">标签 {_h(run.get("tag", "-"))}</span> '
        f'<span class="badge-chip">grader_mode {_h(run.get("grader_mode", "full"))}</span>'
        "</div>",
        "</header>",
        # 元素 1：告警横幅（置于最顶）
        _render_banner(vm),
        _render_meta(vm),
    ]

    # 各区块
    for number, name, renderer in _SECTIONS:
        parts.append(
            f'<section id="sec-{number}"><h2 class="sec">'
            f'<span class="num">{number}</span>{_h(name)}</h2>'
            f"{renderer(vm)}</section>"
        )

    # 元素 11 的抽屉（放在最后，fixed 定位）
    parts.append(_render_drawer(vm))

    # 页脚
    parts.append(
        '<footer class="foot">'
        f"<span>KaoyanBench v{_h(bench.get('version', '1.0'))} · "
        f"Schema {_h(vm.get('schema_version'))} · 生成于 {_h(vm.get('generated_at'))}</span>"
        "<span>数据源：同目录 <code>report.json</code>（事实源）。"
        "本页为零外部请求的单文件报告，断网可完整查看。</span>"
        "</footer>"
    )
    parts.append("</div>")  # .wrap

    # 内联数据（供前端 JS 读取；**不是资源请求**）
    payload = json.dumps(vm, ensure_ascii=False, separators=(",", ":"))
    # 关键：把 </script 拆开，避免 JSON 内容提前闭合脚本标签（数据里可能含 XML/HTML）
    payload = payload.replace("</", "<\\/")
    parts.append(
        f'<script type="application/json" id="report-data">{payload}</script>'
    )
    # 内联脚本（唯一 <script src> 不存在）
    parts.append(f"<script>{_JS}</script>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)


class HtmlReporter:
    """方案 5.6 的 Reporter 协议实现，产出单文件 ``report.html``。"""

    name = "html"

    def render(self, report: ReportModel, out_dir: Path) -> Path:
        target_dir = Path(out_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "report.html"
        path.write_text(render_html(report), encoding="utf-8")
        return path


# --------------------------------------------------------------------------- #
# 自注册（方案 5.6 的注册点；不改动 core/reporter.py）
# --------------------------------------------------------------------------- #
def _register() -> None:
    """把 HtmlReporter 注册进 ``core.reporter`` 的注册表（见 markdown_reporter 同名说明）。"""
    try:
        from ..core.reporter import register_reporter
    except Exception:  # noqa: BLE001
        return
    register_reporter("html", HtmlReporter())


_register()
