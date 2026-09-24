"""手写 SVG 图表生成器（方案 F-03，**零第三方依赖**）。

设计原则
--------
1. **纯标准库**：只拼接字符串，无 matplotlib / 无图表库。
2. **配色交给 CSS 变量**：所有颜色写 ``var(--svg-xxx)``，由 HTML 的
   ``prefers-color-scheme`` 两套变量驱动，因此同一段 SVG 在深浅色下都可读。
   ``<svg>`` 内的 ``fill`` / ``stroke`` 属性**支持** ``var()``，但为兼容性起见，
   多数颜色走 ``style="fill:var(...)"`` 或 class（取决于使用场景，
   本模块提供 ``class_`` 参数让调用方选择）。
3. **可访问性**：每个图表带 ``role="img"`` + ``<title>``/``<desc>`` + ``aria-label``；
   数值不走"颜色唯一编码"，均带文本标签（满足色盲可读）。
4. **数字格式化统一**：:func:`fmt_value` 处理 % / s / K / $ 单位与 ``None``
   （``None`` 一律显示 "—"，**绝不用 0 冒充**，与后端 models.py 的约定一致）。

坐标系约定
----------
所有图表使用「逻辑坐标 = 像素」的 viewBox，``width``/``height`` 由调用方给定；
容器用 CSS ``max-width:100%`` 保证窄屏自适应。
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

__all__ = [
    "escape",
    "fmt_value",
    "fmt_int",
    "fmt_pp",
    "METRIC_UNITS",
    "svg_open",
    "svg_close",
    "stacked_bar",
    "grouped_bars",
    "histogram",
    "horizontal_bars",
    "double_bar",
    "sparkline",
    "palette_class",
]

#: 指标名 → (单位, 是否 0~1 比率)。与后端 ``report.json`` 的 ``summary`` 键一一对应。
METRIC_UNITS: dict[str, tuple[str, bool]] = {
    # 比率类（0~1 → 百分比）
    "task_success_rate": ("%", True),
    "failure_rate": ("%", True),
    "pass_at_1": ("%", True),
    "pass_at_3": ("%", True),
    "factuality_mean": ("%", True),
    "source_precision_mean": ("%", True),
    "search_recall_mean": ("%", True),
    "citation_accuracy_mean": ("%", True),
    "hallucination_rate_mean": ("%", True),
    "tool_success_rate_mean": ("%", True),
    "usage_estimated_ratio": ("%", True),
    "task_success_rate": ("%", True),
    # 分制 / 计数 / 耗时 / 成本
    "score_mean": ("分", False),
    "score_median": ("分", False),
    "score_p90": ("分", False),
    "tool_calls_mean": ("次", False),
    "tool_calls_p90": ("次", False),
    "tokens_mean": ("tokens", False),
    "tokens_p90": ("tokens", False),
    "latency_seconds_mean": ("s", False),
    "latency_seconds_median": ("s", False),
    "latency_seconds_p90": ("s", False),
    "cost_usd_mean": ("$", False),
    "cost_usd_total": ("$", False),
    "cost_usd": ("$", False),
    "latency_seconds": ("s", False),
    "score": ("分", False),
    "usage_missing_count": ("题", False),
}

#: 图表配色槽位（对应 HTML 里的 CSS 变量 ``--svg-c1`` … ``--svg-c8``）。
_SLOTS = 8


def palette_class(index: int) -> str:
    """返回第 ``index`` 个配色槽的 CSS class（循环使用）。"""
    return f"svg-c{(index % _SLOTS) + 1}"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def escape(text: Any) -> str:
    """XML 文本转义（``&`` / ``<`` / ``>`` / ``"`` / ``'``）。

    **所有**写入 SVG 的动态文本都必须过这里，避免答案里的 ``<`` 撑坏 XML。
    """
    return (
        str(text if text is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _round(value: float, digits: int = 2) -> float:
    """稳定四舍五入（避免浮点尾巴撑破 SVG 体积）。"""
    if not math.isfinite(value):
        return 0.0
    return round(value + 0.0, digits)


def fmt_int(value: Any) -> str:
    """整数格式化：``None`` → ``—``；大数加千分位。"""
    if not _is_number(value):
        return "—"
    return f"{int(round(float(value))):,}"


def _fmt_usd(number: float) -> str:
    """美元格式化：**保留有效精度**，避免小额（< $0.01）被显示成 ``$0.00``。

    分档（与 LLM 单次调用的真实量级相符）：

    - ``>= 1000`` → ``$1.2K``（紧凑）
    - ``>= 1``    → ``$37.00``
    - ``>= 0.01`` → ``$0.74``
    - ``< 0.01``  → ``$0.0038``（4 位有效数字）；更小则用 6 位小数

    这样 ``cost_usd_mean = 0.003771`` 会显示为 ``$0.0038`` 而不是误导性的 ``$0.00``。
    """
    magnitude = abs(number)
    if magnitude >= 1000:
        return f"${_fmt_compact(number)}"
    if magnitude >= 1:
        return f"${number:,.2f}"
    if magnitude >= 0.01:
        return f"${number:,.2f}"
    if magnitude > 0:
        # 保留 4 位有效数字：0.003771 → 0.003771（至少 4 位小数）
        text = f"{number:.6f}".rstrip("0")
        if len(text.split(".")[-1]) < 4:
            text = f"{number:.4f}"
        return f"${text}"
    return "$0.00"


def _fmt_compact(value: float) -> str:
    """千分位 / K / M 缩写。"""
    abs_value = abs(value)
    if abs_value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs_value >= 10_000:
        return f"{value / 1_000:.1f}K"
    if abs_value >= 1_000:
        return f"{value:,.0f}"
    return f"{value:,.0f}"


def fmt_value(value: Any, metric: str | None = None, *, digits: int | None = None) -> str:
    """把指标数值格式化为**带单位**的字符串。

    - ``None`` → ``"—"``（未采集，不用 0 冒充）
    - 比率类（``METRIC_UNITS`` 标记 ``ratio=True`` 或 ``metric`` 以 ``_rate`` / ``_mean``
      结尾且值 <= 1）→ 百分比，如 ``78.0%``
    - ``$/tokens`` 走 K/M 缩写，``s`` 保留 1 位小数，``分`` 保留 1 位小数
    """
    suffix, is_ratio = METRIC_UNITS.get(str(metric), ("", False))

    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"

    if not _is_number(value):
        text = str(value)
        return text if text else "—"

    number = float(value)
    if not math.isfinite(number):
        return "—"

    if is_ratio:
        return f"{number * 100:.1f}%"

    if suffix == "$":
        return _fmt_usd(number)
    if suffix == "s":
        return f"{number:,.1f}s" if number >= 1 else f"{number:,.2f}s"
    if suffix == "分":
        return f"{number:,.1f}分"
    if suffix == "次":
        return f"{number:,.0f}次"
    if suffix == "题":
        return f"{number:,.0f}题"
    if suffix == "tokens":
        return f"{_fmt_compact(number)}"

    if digits is not None:
        return f"{number:,.{digits}f}"
    if abs(number - round(number)) < 1e-9:
        return _fmt_compact(number)
    return f"{number:,.2f}"


def fmt_pp(delta_pp: Any, *, digits: int = 1) -> str:
    """Δ 百分点格式化：``+5.0pp`` / ``-3.2pp`` / ``—``。"""
    if not _is_number(delta_pp):
        return "—"
    number = float(delta_pp)
    sign = "+" if number > 0 else ""
    return f"{sign}{number:.{digits}f}pp"


# --------------------------------------------------------------------------- #
# 数值刻度（"漂亮"的坐标轴）
# --------------------------------------------------------------------------- #
def _nice_step(span: float, target_ticks: int = 4) -> float:
    """返回适合 ``span`` 的"漂亮"步长（1/2/2.5/5 × 10^n）。"""
    if span <= 0:
        return 1.0
    raw = span / max(1, target_ticks)
    magnitude = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 2.5, 5, 10):
        if raw <= factor * magnitude:
            return factor * magnitude
    return 10 * magnitude


def _ticks(max_value: float, target_ticks: int = 4) -> tuple[float, list[float]]:
    """返回 ``(轴上限, 刻度列表)``；上限 ≥ max_value。"""
    if max_value <= 0:
        return 1.0, [0.0, 0.5, 1.0]
    step = _nice_step(max_value, target_ticks)
    top = math.ceil(max_value / step) * step
    if top <= 0:
        top = step
    values: list[float] = []
    cursor = 0.0
    # 最多 8 个刻度，防御 step 过小
    while cursor <= top + step / 2 and len(values) < 12:
        values.append(_round(cursor, 6))
        cursor += step
    return top, values


# --------------------------------------------------------------------------- #
# SVG 外壳
# --------------------------------------------------------------------------- #
def svg_open(
    width: float,
    height: float,
    *,
    title: str,
    desc: str = "",
    class_: str = "chart",
    extra: str = "",
) -> str:
    """开始一个 SVG；返回的字符串**未闭合**，需配合 :func:`svg_close`。

    带 ``role="img"`` + ``aria-label``，屏幕阅读器可读；``preserveAspectRatio``
    保证窄屏缩放不变形。
    """
    label = escape(title)
    parts = [
        f'<svg class="{escape(class_)}" role="img" aria-label="{label}" '
        f'viewBox="0 0 {_round(width, 2)} {_round(height, 2)}" '
        f'width="{_round(width, 2)}" height="{_round(height, 2)}" '
        f'preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg"'
    ]
    if extra:
        parts.append(f" {extra}")
    parts.append(">")
    parts.append(f"<title>{label}</title>")
    if desc:
        parts.append(f"<desc>{escape(desc)}</desc>")
    return "".join(parts)


def svg_close() -> str:
    return "</svg>"


def _text(
    x: float,
    y: float,
    content: Any,
    *,
    class_: str = "svg-label",
    anchor: str = "start",
    baseline: str = "middle",
    size: float | None = None,
    weight: str | None = None,
) -> str:
    style = ""
    if size is not None:
        style += f"font-size:{_round(size, 2)}px;"
    if weight is not None:
        style += f"font-weight:{weight};"
    style_attr = f' style="{style}"' if style else ""
    return (
        f'<text x="{_round(x, 2)}" y="{_round(y, 2)}" class="{class_}" '
        f'text-anchor="{anchor}" dominant-baseline="{baseline}"{style_attr}>'
        f"{escape(content)}</text>"
    )


def _rect(
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    class_: str = "svg-bar",
    radius: float = 0.0,
    opacity: float | None = None,
) -> str:
    if width <= 0 or height <= 0:
        return ""
    attrs = [
        f'x="{_round(x, 2)}"',
        f'y="{_round(y, 2)}"',
        f'width="{_round(width, 2)}"',
        f'height="{_round(height, 2)}"',
    ]
    if radius:
        attrs.append(f'rx="{_round(radius, 2)}"')
    if opacity is not None:
        attrs.append(f'opacity="{_round(opacity, 3)}"')
    return f'<rect class="{escape(class_)}" {" ".join(attrs)}/>'


def _line(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    class_: str = "svg-axis",
    dash: str | None = None,
) -> str:
    dash_attr = f' stroke-dasharray="{escape(dash)}"' if dash else ""
    return (
        f'<line class="{escape(class_)}" x1="{_round(x1, 2)}" y1="{_round(y1, 2)}" '
        f'x2="{_round(x2, 2)}" y2="{_round(y2, 2)}"{dash_attr}/>'
    )


def _empty_state(width: float, height: float, message: str = "暂无数据") -> str:
    """空状态：图表区域给出可读提示（不画假柱子）。"""
    return (
        _rect(0, 0, width, height, class_="svg-empty", radius=6)
        + _text(width / 2, height / 2, message, class_="svg-empty-text", anchor="middle")
    )


# --------------------------------------------------------------------------- #
# 1) 堆叠条（总分构成）
# --------------------------------------------------------------------------- #
def stacked_bar(
    segments: Sequence[tuple[str, float, float]],
    *,
    width: float = 720,
    height: float = 128,
    title: str = "堆叠条",
    max_total: float = 100.0,
    show_total: bool = True,
) -> str:
    """横向堆叠条。

    参数
    ----
    segments : ``[(标签, 数值, 该项满分), ...]``，按给定顺序从左到右堆叠。
               第三项用于在图例里显示 ``实得/满分``（满分之和应等于 ``max_total``）。
    max_total : 整条的长度对应的数值（分数制默认 100）。
    show_total : 是否在条尾显示总分。

    说明：**未得分部分**用 ``svg-track`` 画一条底槽，直观看出"丢了哪些分"。
    """
    pad_left, pad_right, pad_top = 8.0, 8.0, 26.0
    bar_h = 30.0
    legend_row_h = 20.0
    labels = [(str(name), float(value or 0.0), float(full or 0.0)) for name, value, full in segments]
    total_earned = sum(v for _, v, _ in labels)
    has_data = any(v > 0 for _, v, _ in labels) or sum(full for _, _, full in labels) > 0

    # 动态高度：条 + 图例（每行 4 项，窄屏靠 CSS 折行）
    legend_cols = 4
    legend_rows = max(1, math.ceil(len(labels) / legend_cols))
    needed = pad_top + bar_h + 14 + legend_rows * legend_row_h + 8
    height = max(float(height), needed)

    inner_w = max(40.0, float(width) - pad_left - pad_right)
    out: list[str] = [
        svg_open(
            width,
            height,
            title=title,
            desc="总分构成堆叠条：" + "、".join(f"{n} {v:g}/{f:g}" for n, v, f in labels),
            class_="chart chart-stacked",
        )
    ]
    if not has_data:
        out.append(_empty_state(float(width), height, "暂无评分构成数据"))
        out.append(svg_close())
        return "".join(out)

    scale = inner_w / max_total if max_total > 0 else 0.0
    y = pad_top
    out.append(_rect(pad_left, y, inner_w, bar_h, class_="svg-track", radius=4))

    cursor = pad_left
    for index, (name, value, full) in enumerate(labels):
        seg_w = max(0.0, value) * scale
        if seg_w <= 0:
            continue
        out.append(
            _rect(cursor, y, seg_w, bar_h, class_=palette_class(index), radius=0)
        )
        # 段内标签：宽度够才写字，避免糊成一团
        if seg_w >= 34:
            out.append(
                _text(
                    cursor + seg_w / 2,
                    y + bar_h / 2,
                    f"{value:g}",
                    class_="svg-bar-value",
                    anchor="middle",
                    size=11,
                    weight="600",
                )
            )
        cursor += seg_w

    if show_total:
        out.append(
            _text(
                pad_left + inner_w,
                y - 12,
                f"总分 {total_earned:g}/{max_total:g}",
                class_="svg-total",
                anchor="end",
                size=12,
                weight="700",
            )
        )

    # 图例
    legend_y = y + bar_h + 18
    col_w = inner_w / legend_cols
    for index, (name, value, full) in enumerate(labels):
        row, col = divmod(index, legend_cols)
        lx = pad_left + col * col_w
        ly = legend_y + row * legend_row_h
        out.append(_rect(lx, ly - 5, 10, 10, class_=palette_class(index), radius=2))
        out.append(
            _text(
                lx + 15,
                ly,
                f"{name} {value:g}/{full:g}",
                class_="svg-legend",
                size=11,
            )
        )

    out.append(svg_close())
    return "".join(out)


# --------------------------------------------------------------------------- #
# 2) 分组柱（类别能力对比）
# --------------------------------------------------------------------------- #
def grouped_bars(
    groups: Sequence[str],
    series: Sequence[tuple[str, Sequence[float | None], str]],
    *,
    width: float = 720,
    height: float = 280,
    title: str = "分组柱状图",
    y_max: float = 1.0,
    y_suffix: str = "%",
    as_ratio: bool = True,
    y_flip: bool = False,
) -> str:
    """分组柱状图：``groups`` 为 X 轴（类别），``series`` 为每组的多个系列。

    参数
    ----
    groups : X 轴标签（用后端 ``by_category[].label``，中文）。
    series : ``[(系列名, [值...], 单位后缀), ...]``；值 ``None`` 表示未采集 → 不画柱。
    y_max  : Y 轴满量程（比率类填 1.0）。
    as_ratio : 值是否为 0~1 比率（决定标签是 ``x100`` 还是原样）。
    """
    groups = [str(g) for g in groups]
    n_groups = len(groups)
    pad_left, pad_right, pad_top, pad_bottom = 52.0, 12.0, 30.0, 58.0
    plot_w = max(40.0, float(width) - pad_left - pad_right)
    plot_h = max(40.0, float(height) - pad_top - pad_bottom)

    out: list[str] = [
        svg_open(
            width,
            height,
            title=title,
            desc=f"按 {'/'.join(str(g) for g in groups)} 分组的 {len(series)} 个系列对比",
            class_="chart chart-grouped",
        )
    ]
    if not n_groups or not series:
        out.append(_empty_state(float(width), height, "暂无类别数据"))
        out.append(svg_close())
        return "".join(out)

    axis_top, ticks = _ticks(float(y_max), 4)
    base_y = pad_top + plot_h

    # 网格 + Y 轴刻度
    for tick in ticks:
        ratio = tick / axis_top if axis_top else 0
        ty = base_y - ratio * plot_h
        out.append(_line(pad_left, ty, pad_left + plot_w, ty, class_="svg-grid", dash="3 3"))
        out.append(
            _text(
                pad_left - 8,
                ty,
                (f"{tick * 100:.0f}%" if as_ratio else f"{tick:g}{y_suffix}"),
                class_="svg-tick",
                anchor="end",
                size=10,
            )
        )
    out.append(_line(pad_left, base_y, pad_left + plot_w, base_y, class_="svg-axis"))

    group_w = plot_w / n_groups
    n_series = max(1, len(series))
    bar_w = min(26.0, max(6.0, (group_w * 0.68) / n_series))

    for gi, group in enumerate(groups):
        gx = pad_left + gi * group_w
        cluster_w = bar_w * n_series
        start_x = gx + (group_w - cluster_w) / 2
        for si, (sname, values, _suffix) in enumerate(series):
            raw = values[gi] if gi < len(values) else None
            if not _is_number(raw):
                continue
            value = float(raw)
            ratio = min(1.0, max(0.0, value / axis_top)) if axis_top else 0.0
            bar_h = ratio * plot_h
            bx = start_x + si * bar_w
            by = base_y - bar_h
            out.append(
                _rect(bx, by, bar_w - 2, bar_h, class_=palette_class(si), radius=3)
            )
            label = f"{value * 100:.0f}%" if as_ratio else f"{value:.2f}".rstrip("0").rstrip(".")
            out.append(
                _text(
                    bx + (bar_w - 2) / 2,
                    by - 8,
                    label,
                    class_="svg-bar-value",
                    anchor="middle",
                    size=10,
                )
            )
        # X 轴标签（中文类别名可能长 → 折两行）
        lines = _wrap_label(group, max_chars=6, max_lines=2)
        for li, line in enumerate(lines):
            out.append(
                _text(
                    gx + group_w / 2,
                    base_y + 16 + li * 13,
                    line,
                    class_="svg-axis-label",
                    anchor="middle",
                    size=11,
                )
            )

    # 图例（顶部）
    lx = pad_left
    for si, (sname, _values, suffix) in enumerate(series):
        text = f"{sname}{f'（{suffix}）' if suffix else ''}"
        out.append(_rect(lx, pad_top - 22, 10, 10, class_=palette_class(si), radius=2))
        out.append(_text(lx + 15, pad_top - 16, text, class_="svg-legend", size=11))
        lx += 19 + len(text) * 11.5

    out.append(svg_close())
    return "".join(out)


def _wrap_label(text: str, *, max_chars: int = 6, max_lines: int = 2) -> list[str]:
    """极简中文折行：按字符数切，末行加省略号。"""
    text = str(text)
    if len(text) <= max_chars:
        return [text]
    lines: list[str] = []
    cursor = 0
    while cursor < len(text) and len(lines) < max_lines:
        lines.append(text[cursor : cursor + max_chars])
        cursor += max_chars
    if cursor < len(text) and lines:
        lines[-1] = lines[-1][: max_chars - 1] + "…"
    return lines


# --------------------------------------------------------------------------- #
# 3) 直方图（Latency 分布 + P50/P90 标线）
# --------------------------------------------------------------------------- #
def histogram(
    bins: Sequence[tuple[str, int]],
    *,
    width: float = 720,
    height: float = 260,
    title: str = "直方图",
    markers: Sequence[tuple[str, float, int]] = (),
    unit: str = "s",
) -> str:
    """直方图 + 参考标线。

    参数
    ----
    bins : ``[(区间标签, 计数), ...]``，由前端对 ``tasks[].metrics.latency_seconds`` 分桶。
    markers : ``[(名称, 数值, 所属桶下标), ...]``，如 ``[("P50", 142.0, 3), ("P90", 388.0, 6)]``，
              标线画在对应桶的**左边界**（或末桶右边界）。
    """
    bins = [(str(label), int(count or 0)) for label, count in bins]
    pad_left, pad_right, pad_top, pad_bottom = 48.0, 16.0, 30.0, 52.0
    plot_w = max(40.0, float(width) - pad_left - pad_right)
    plot_h = max(40.0, float(height) - pad_top - pad_bottom)

    out: list[str] = [
        svg_open(
            width,
            height,
            title=title,
            desc="分布直方图，区间：" + "、".join(f"{l}({c})" for l, c in bins),
            class_="chart chart-histogram",
        )
    ]
    total = sum(c for _, c in bins)
    if not bins or total == 0:
        out.append(_empty_state(float(width), height, "暂无分布数据"))
        out.append(svg_close())
        return "".join(out)

    axis_top, ticks = _ticks(float(max(c for _, c in bins)), 4)
    base_y = pad_top + plot_h
    for tick in ticks:
        ty = base_y - (tick / axis_top if axis_top else 0) * plot_h
        out.append(_line(pad_left, ty, pad_left + plot_w, ty, class_="svg-grid", dash="3 3"))
        out.append(
            _text(pad_left - 8, ty, f"{tick:g}", class_="svg-tick", anchor="end", size=10)
        )
    out.append(_line(pad_left, base_y, pad_left + plot_w, base_y, class_="svg-axis"))

    slot_w = plot_w / max(1, len(bins))
    bar_w = max(4.0, slot_w * 0.74)
    for index, (label, count) in enumerate(bins):
        bar_h = (count / axis_top if axis_top else 0) * plot_h
        bx = pad_left + index * slot_w + (slot_w - bar_w) / 2
        by = base_y - bar_h
        out.append(_rect(bx, by, bar_w, bar_h, class_=palette_class(0), radius=3))
        if count:
            out.append(
                _text(
                    bx + bar_w / 2,
                    by - 7,
                    str(count),
                    class_="svg-bar-value",
                    anchor="middle",
                    size=10,
                )
            )
        out.append(
            _text(
                bx + bar_w / 2,
                base_y + 15,
                label,
                class_="svg-axis-label",
                anchor="middle",
                size=10,
            )
        )

    # P50 / P90 标线
    for name, value, bin_index in markers:
        if not _is_number(value):
            continue
        slot = min(max(int(bin_index), 0), max(0, len(bins) - 1))
        mx = pad_left + slot * slot_w
        out.append(_line(mx, pad_top - 4, mx, base_y, class_="svg-marker", dash="5 3"))
        out.append(
            _text(
                mx + 4,
                pad_top - 8,
                f"{name} {value:,.0f}{unit}",
                class_="svg-marker-label",
                size=10,
                weight="600",
            )
        )

    out.append(svg_close())
    return "".join(out)


# --------------------------------------------------------------------------- #
# 4) 横向条（成本 TOP10 / 错误分类）
# --------------------------------------------------------------------------- #
def horizontal_bars(
    items: Sequence[tuple[str, float | None, str]],
    *,
    width: float = 720,
    row_height: float = 26.0,
    title: str = "横向条",
    value_formatter: Any | None = None,
    color_index: int | None = None,
    max_rows: int | None = None,
    label_width: float = 190.0,
) -> str:
    """横向条形图（成本 TOP10、错误分类共用）。

    参数
    ----
    items : ``[(标签, 数值, 数值文本), ...]``；数值 ``None`` 时用 ``数值文本`` 占位。
    value_formatter : 可选的 ``callable(value) -> str``，用于柱尾标签。
    color_index : 指定单色槽（如错误分类统一色）；``None`` 则按行循环配色。
    max_rows : 截断行数（TOP10 场景）。
    """
    rows = list(items)
    if max_rows is not None:
        rows = rows[: max(0, int(max_rows))]
    label_width = float(label_width)
    pad_left, pad_right, pad_top, pad_bottom = 8.0, 74.0, 26.0, 10.0
    plot_left = pad_left + label_width
    plot_w = max(40.0, float(width) - pad_left - pad_right - label_width)
    height = pad_top + pad_bottom + max(1, len(rows)) * row_height

    out: list[str] = [
        svg_open(
            width,
            height,
            title=title,
            desc="横向条形图：" + "、".join(str(label) for label, _, _ in rows[:10]),
            class_="chart chart-hbars",
        )
    ]
    if not rows:
        out.append(_empty_state(float(width), max(60.0, height), "暂无数据"))
        out.append(svg_close())
        return "".join(out)

    numeric = [float(v) for _, v, _ in rows if _is_number(v)]
    peak = max(numeric) if numeric else 0.0
    bar_h = row_height * 0.62

    for index, (label, value, value_text) in enumerate(rows):
        ry = pad_top + index * row_height
        out.append(
            _text(
                pad_left,
                ry + bar_h / 2 + 1,
                _truncate(str(label), 14),
                class_="svg-axis-label",
                size=11,
            )
        )
        out.append(
            _rect(plot_left, ry, plot_w, bar_h, class_="svg-track", radius=3, opacity=0.45)
        )
        if _is_number(value) and peak > 0:
            ratio = max(0.0, float(value)) / peak
            out.append(
                _rect(
                    plot_left,
                    ry,
                    max(1.5, ratio * plot_w),
                    bar_h,
                    class_=palette_class(color_index if color_index is not None else index),
                    radius=3,
                )
            )
        shown = value_formatter(value) if value_formatter and _is_number(value) else value_text
        out.append(
            _text(
                plot_left + plot_w + 8,
                ry + bar_h / 2 + 1,
                shown,
                class_="svg-bar-value",
                size=11,
                weight="600",
            )
        )

    out.append(svg_close())
    return "".join(out)


def _truncate(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


# --------------------------------------------------------------------------- #
# 5) 双条对比（Pass@1 vs Pass@3）
# --------------------------------------------------------------------------- #
def double_bar(
    pairs: Sequence[tuple[str, float | None, float | None]],
    *,
    width: float = 720,
    height: float = 132,
    title: str = "双条对比",
    as_ratio: bool = True,
    y_max: float = 1.0,
) -> str:
    """同一行两条（如 Pass@1 / Pass@3）的对比条。

    ``pairs`` : ``[(行标签, 系列A值, 系列B值), ...]``（本报告只有一行，
    但保留多行以复用）。
    """
    rows = [(str(a), b, c) for a, b, c in pairs]
    pad_left, pad_right, pad_top = 8.0, 8.0, 30.0
    row_h = 42.0
    height = max(float(height), pad_top + max(1, len(rows)) * row_h + 16)
    plot_w = max(40.0, float(width) - pad_left - pad_right)

    out: list[str] = [
        svg_open(
            width,
            height,
            title=title,
            desc="；".join(f"{a}: {b} / {c}" for a, b, c in rows),
            class_="chart chart-double",
        )
    ]
    if not rows:
        out.append(_empty_state(float(width), height, "暂无数据"))
        out.append(svg_close())
        return "".join(out)

    # 图例
    out.append(_rect(pad_left, pad_top - 22, 10, 10, class_=palette_class(0), radius=2))
    out.append(_text(pad_left + 15, pad_top - 16, "Pass@1", class_="svg-legend", size=11))
    out.append(_rect(pad_left + 96, pad_top - 22, 10, 10, class_=palette_class(1), radius=2))
    out.append(_text(pad_left + 111, pad_top - 16, "Pass@3", class_="svg-legend", size=11))

    for ri, (row_label, first, second) in enumerate(rows):
        base_y = pad_top + ri * row_h
        for si, value in enumerate((first, second)):
            by = base_y + si * 16
            out.append(_rect(pad_left, by, plot_w, 13, class_="svg-track", radius=3, opacity=0.45))
            text = "—"
            if _is_number(value):
                ratio = min(1.0, max(0.0, float(value) / y_max)) if y_max else 0
                out.append(
                    _rect(pad_left, by, max(2.0, ratio * plot_w), 13, class_=palette_class(si), radius=3)
                )
                text = f"{float(value) * 100:.1f}%" if as_ratio else f"{float(value):,.2f}"
            else:
                out.append(
                    _rect(pad_left, by, 0, 13, class_=palette_class(si), radius=3)
                )
            out.append(
                _text(
                    pad_left + plot_w - 6,
                    by + 6.5,
                    text,
                    class_="svg-bar-value",
                    anchor="end",
                    size=11,
                    weight="700",
                )
            )
        if len(rows) > 1:
            out.append(
                _text(pad_left, base_y + 34, row_label, class_="svg-axis-label", size=11)
            )

    out.append(svg_close())
    return "".join(out)


# --------------------------------------------------------------------------- #
# 6) 迷你趋势线（指标卡内可选使用）
# --------------------------------------------------------------------------- #
def sparkline(
    values: Iterable[float],
    *,
    width: float = 96,
    height: float = 26,
    title: str = "趋势",
) -> str:
    """迷你折线（不画坐标轴），用于指标卡角标。值不足 2 个时返回空字符串。"""
    series = [float(v) for v in values if _is_number(v)]
    if len(series) < 2:
        return ""
    lo, hi = min(series), max(series)
    span = (hi - lo) or 1.0
    step = width / (len(series) - 1)
    points = " ".join(
        f"{_round(i * step, 2)},{_round(height - (v - lo) / span * (height - 4) - 2, 2)}"
        for i, v in enumerate(series)
    )
    return (
        svg_open(width, height, title=title, class_="chart chart-spark")
        + f'<polyline class="svg-spark-line" points="{points}"/>'
        + svg_close()
    )
