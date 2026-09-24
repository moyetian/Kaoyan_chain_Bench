#!/usr/bin/env python3
"""``report.html`` 渲染验收脚本（方案 5.7 的 12 类元素 + 单文件硬约束）。

用途
----
对 ``HtmlReporter`` 产出的单文件 HTML 做**离线静态校验**，无需浏览器、无第三方依赖：

1. **12 类元素存在性**：逐项断言关键 DOM id/class（复用
   ``kaoyanbench.reporters.html_reporter.ELEMENT_SELECTORS``，与实现同源，杜绝两处漂移）。
2. **零外部请求**：断言无 ``<link>`` / ``src="`` / ``@import`` / ``url(`` /
   ``//cdn`` / 外部 ``http(s)://``（**唯一允许**：SVG 命名空间
   ``http://www.w3.org/2000/svg``，它不是网络请求）。
   URL 扫描**排除 ``<script>`` 块内的数据**（内嵌 JSON 里的来源 URL 是报告内容，
   不产生请求），另以「script 无主动外联」断言覆盖 fetch/XHR/WebSocket 等
   真正会发起网络请求的 API。
3. **HTML 结构**：用标准库 :mod:`html.parser` 解析，检查标签闭合、
   记录未转义的裸 ``<``（文本里出现 ``<`` 会让解析器把它当标签起始）。
4. **内联资源**：断言存在 ``<style>`` 与 ``<script>`` 内联块、且无 ``<script src=``。

用法::

    # 校验一个已生成的 report.html
    python tools/check_report_html.py reports/core50__mock__v1/report.html

    # 直接从 report.json 现场渲染并校验（推荐，顺带做一次渲染冒烟）
    python tools/check_report_html.py --from-json reports/core50__mock__v1/report.json

退出码：0 = 全部 PASS；1 = 存在 FAIL（打印逐项清单）。
"""

from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

# 允许把仓库 src 加进 sys.path（脚本可直接运行，无需先安装）
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# --------------------------------------------------------------------------- #
# 0) 12 类元素选择器：与 HtmlReporter 同源
# --------------------------------------------------------------------------- #
def _element_selectors() -> dict[int, tuple[str, str]]:
    from kaoyanbench.reporters.html_reporter import ELEMENT_SELECTORS

    return dict(ELEMENT_SELECTORS)


#: 把选择器（``#id`` / ``#id class`` / ``#id .cls`` / ``#id svg.cls``）拆成可断言片段。
#: 返回 ``[(kind, token), ...]``，kind ∈ {"id", "class", "tag_class"}。
def _selector_tokens(selector: str) -> list[tuple[str, str]]:
    """把一条简化选择器解析成若干断言 token。

    支持的写法（够用且无歧义）：
    - ``#foo``               → id=foo
    - ``#foo .bar``          → id=foo 且存在 class=bar
    - ``#foo svg.bar``       → id=foo 且存在 ``<svg class="… bar …">``
    - ``#foo tbody#bar``     → id=foo 且 id=bar
    """
    tokens: list[tuple[str, str]] = []
    for part in selector.split():
        if part.startswith("#"):
            tokens.append(("id", part[1:]))
        elif part.startswith("."):
            tokens.append(("class", part[1:]))
        elif "#" in part:
            # 形如 ``tbody#task-table-body``：只断言 id（标签名不额外约束，避免过严）
            _tag, ident = part.split("#", 1)
            tokens.append(("id", ident))
        elif "." in part:
            tag, cls = part.split(".", 1)
            tokens.append(("tag_class", f"{tag}|{cls}"))
        else:
            # 裸标签名（如 ``svg``）——顺带断言存在
            tokens.append(("tag", part))
    return tokens


class _ElementChecker:
    """基于正则的"存在性"检查器（对生成期模板足够稳，避免引入 DOM 依赖）。

    说明：这里刻意**不用** html.parser 做结构断言（结构断言在 :class:`_WellFormedParser`
    里单独做），因为我们要断言的是"渲染产物里确实有这些 id/class"，
    正则对固定模板最直接，且不依赖浏览器级容错。
    """

    def __init__(self, html: str) -> None:
        self.html = html
        self._ids = set(re.findall(r'\bid="([^"]+)"', html))
        self._classes: set[str] = set()
        for cls in re.findall(r'\bclass="([^"]*)"', html):
            for name in cls.split():
                self._classes.add(name.strip())
        self._svg_class_tags: set[str] = set()
        for tag, cls in re.findall(r"<(svg)\s[^>]*\bclass=\"([^\"]*)\"", html):
            for name in cls.split():
                self._svg_class_tags.add(f"{tag}|{name.strip()}")
        self._tags = set(re.findall(r"<([a-zA-Z][a-zA-Z0-9]*)\b", html))

    def has_token(self, kind: str, token: str) -> bool:
        if kind == "id":
            return token in self._ids
        if kind == "class":
            return token in self._classes
        if kind == "tag_class":
            tag, cls = token.split("|", 1)
            if tag == "svg":
                return token in self._svg_class_tags
            return token in self._svg_class_tags or cls in self._classes
        if kind == "tag":
            return token in self._tags
        return False


# --------------------------------------------------------------------------- #
# 1) 零外部请求
# --------------------------------------------------------------------------- #
_NS_SVG = "http://www.w3.org/2000/svg"
_CDN_PATTERNS: tuple[tuple[str, str], ...] = (
    ("<link", r"<link\b"),
    ("<script src=", r"<script\b[^>]*\bsrc\s*="),
    ("<img src=", r"<img\b"),
    ("<iframe", r"<iframe\b"),
    ("@import", r"@import\b"),
    ("url(", r"url\("),
    ("//cdn", r"//cdn"),
    ("http(s)://", r"https?://"),
    ("// 协议相对 URL", r"[\"'(]//[a-zA-Z0-9]"),
)

#: script 内的主动外联 API：一旦出现即可能发起网络请求（与 URL 文本不同）。
_ACTIVE_NET_PATTERNS: tuple[str, ...] = (
    "fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "new Image(",
)


def _strip_script_contents(html: str) -> str:
    """剥离 ``<script>`` 块内容（保留开闭标签），用于 URL 检查。

    script 内的字符串（如报告内嵌的 JSON 数据）**不会**自动发起网络请求；
    对其做 URL 扫描会把任务数据里的来源 URL（如 ``https://yz.chsi.com.cn/...``）
    误判为外部资源。主动外联由 `_check_active_network` 单独覆盖。
    """
    return re.sub(
        r"(<script\b[^>]*>).*?(</script>)", r"\1\2", html, flags=re.DOTALL | re.IGNORECASE
    )


def _check_active_network(html: str) -> tuple[bool, str, str]:
    """script 内不得出现主动外联 API（fetch/XHR/WebSocket/...）。"""
    hits: list[str] = []
    for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", html, flags=re.DOTALL | re.IGNORECASE):
        body = m.group(1)
        hits.extend(pat for pat in _ACTIVE_NET_PATTERNS if pat in body)
    ok = not hits
    return (ok, "script 无主动外联", "未出现" if ok else f"出现：{sorted(set(hits))}")


def _check_no_external(html: str) -> list[tuple[bool, str, str]]:
    """返回 ``[(ok, 名称, 说明), ...]``。"""
    results: list[tuple[bool, str, str]] = []
    # URL 扫描排除 script 数据块（见 `_strip_script_contents` 说明）
    scan = _strip_script_contents(html)

    for name, pattern in _CDN_PATTERNS:
        hits = re.findall(pattern, scan, flags=re.IGNORECASE)
        if name == "http(s)://":
            # 唯一允许：SVG 命名空间（XML 标识符，不发起请求）
            bad = []
            for m in re.finditer(pattern, scan, flags=re.IGNORECASE):
                frag = scan[m.start() : m.start() + len(_NS_SVG) + 4]
                if frag.startswith(_NS_SVG):
                    continue
                bad.append(scan[max(0, m.start() - 20) : m.start() + 60])
            ok = not bad
            detail = f"仅 SVG 命名空间 {_NS_SVG}（允许）" if ok else f"发现外部地址：{bad[:3]}"
            results.append((ok, name, detail))
            continue
        ok = len(hits) == 0
        results.append((ok, name, "未出现" if ok else f"出现 {len(hits)} 次"))

    results.append(_check_active_network(html))

    # 存在内联 <style> 与 <script>（保证样式/行为内嵌）
    results.append(("<style>" in html, "<style> 内联", "存在" if "<style>" in html else "缺失"))
    results.append(("<script>" in html, "<script> 内联", "存在" if "<script>" in html else "缺失"))
    return results


# --------------------------------------------------------------------------- #
# 2) HTML 结构（标准库 html.parser）
# --------------------------------------------------------------------------- #
#: void 元素（HTML 里不要求闭合标签）
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


class _WellFormedParser(HTMLParser):
    """轻量结构检查：标签配对 + 记录解析器把文本误当标签的情况。

    - 维护一个**开放标签栈**，遇到结束标签时校验与栈顶匹配；
    - 记录所有结束标签，若结束时栈非空 → 存在未闭合标签；
    - 记录 ``handle_startendtag`` 与 ``handle_data`` 中疑似未转义 ``<`` 的片段。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.mismatches: list[str] = []
        self.unclosed: list[str] = []
        self.unknown_start: list[str] = []  # 栈为空时出现的结束标签
        self.text_with_lt: list[str] = []
        # ``<script>`` / ``<style>`` 的内容是 CDATA：其中的 ``<`` 是合法数据
        # （JS 比较运算、JSON 字符串里的 "<"），**不算**未转义。
        self._cdata_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _VOID_TAGS:
            return
        if tag in ("script", "style"):
            self._cdata_depth += 1
        self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        # 自闭合（如 <rect .../>）——不压栈
        return

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if tag in ("script", "style") and self._cdata_depth > 0:
            self._cdata_depth -= 1
        if not self.stack:
            self.unknown_start.append(tag)
            return
        # 允许浏览器式容错：回溯查找匹配项；若栈顶不匹配则记录 mismatch
        if self.stack[-1] == tag:
            self.stack.pop()
            return
        if tag in self.stack:
            # 中间有未闭合标签：记录后一路弹到匹配项
            idx = len(self.stack) - 1 - self.stack[::-1].index(tag)
            dangling = self.stack[idx + 1 :]
            if dangling:
                self.mismatches.append(f"</{tag}> 之前有未闭合：{dangling}")
            del self.stack[idx:]
        else:
            self.mismatches.append(f"出现无对应开标签的 </{tag}>")

    def handle_data(self, data: str) -> None:
        # ``&lt;`` 已被 convert_charrefs 解析；若数据里仍有裸 ``<``，
        # 说明作者没转义（解析器在多数情况下会把它当标签，此处主要是兜底统计）。
        # ``<script>`` / ``<style>`` 处于 CDATA 模式，其中的 ``<`` 合法，跳过。
        if self._cdata_depth > 0:
            return
        if "<" in data:
            self.text_with_lt.append(data.strip()[:80])

    def report(self) -> dict[str, Any]:
        return {
            "unclosed": list(self.stack),
            "mismatches": list(self.mismatches),
            "unknown_end": list(self.unknown_start),
            "text_with_lt": list(self.text_with_lt)[:5],
        }


def _check_structure(html: str) -> tuple[list[tuple[bool, str, str]], dict[str, Any]]:
    parser = _WellFormedParser()
    parser.feed(html)
    parser.close()
    rep = parser.report()
    results: list[tuple[bool, str, str]] = []
    results.append(
        (
            not rep["unclosed"],
            "标签闭合",
            "全部闭合" if not rep["unclosed"] else f"未闭合：{rep['unclosed'][:6]}",
        )
    )
    results.append(
        (
            not rep["mismatches"],
            "标签配对",
            "无错配" if not rep["mismatches"] else f"错配：{rep['mismatches'][:4]}",
        )
    )
    results.append(
        (
            not rep["unknown_end"],
            "无多余结束标签",
            "无" if not rep["unknown_end"] else f"多余：{rep['unknown_end'][:6]}",
        )
    )
    results.append(
        (
            not rep["text_with_lt"],
            "文本无未转义 <",
            "无" if not rep["text_with_lt"] else f"疑似：{rep['text_with_lt'][:3]}",
        )
    )
    return results, rep


# --------------------------------------------------------------------------- #
# 3) 主流程
# --------------------------------------------------------------------------- #
def _render_from_json(path: Path) -> str:
    from kaoyanbench.core.models import ReportModel
    from kaoyanbench.reporters.html_reporter import render_html

    import json

    raw = json.loads(path.read_text(encoding="utf-8"))
    return render_html(ReportModel.from_dict(raw))


def _line(ok: bool | Any, tag: str, name: str, detail: str = "") -> str:
    mark = "PASS" if ok else "FAIL"
    suffix = f"  — {detail}" if detail else ""
    return f"  [{mark}] {tag:<2} {name}{suffix}"


def check_html(html: str) -> tuple[int, int]:
    """执行全部校验；返回 ``(passed, failed)`` 并打印清单。"""
    selectors = _element_selectors()
    checker = _ElementChecker(html)
    passed = failed = 0

    print("=" * 78)
    print("KaoyanBench report.html 验收（12 类元素 + 单文件硬约束）")
    print("=" * 78)

    print("\n[1] 12 类必需元素")
    for index in sorted(selectors):
        label, selector = selectors[index]
        tokens = _selector_tokens(selector)
        missing = [f"{kind}:{tok}" for kind, tok in tokens if not checker.has_token(kind, tok)]
        ok = not missing
        if ok:
            passed += 1
        else:
            failed += 1
        print(_line(ok, str(index), f"{label}  <{selector}>", "存在" if ok else f"缺失 {missing}"))

    print("\n[2] 零外部请求 / 内联资源")
    for ok, name, detail in _check_no_external(html):
        if ok:
            passed += 1
        else:
            failed += 1
        print(_line(ok, "·", name, detail))

    print("\n[3] HTML 结构（标准库 html.parser）")
    struct_results, _rep = _check_structure(html)
    for ok, name, detail in struct_results:
        if ok:
            passed += 1
        else:
            failed += 1
        print(_line(ok, "·", name, detail))

    print("\n" + "-" * 78)
    total = passed + failed
    print(f"汇总：{passed}/{total} PASS，{failed} FAIL")
    print("-" * 78)
    return passed, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验 report.html 的 12 类元素与单文件硬约束",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("html", nargs="?", help="待校验的 report.html 路径")
    parser.add_argument(
        "--from-json",
        metavar="REPORT_JSON",
        help="从 report.json 现场渲染后再校验（顺带做一次渲染冒烟）",
    )
    args = parser.parse_args(argv)

    if args.from_json:
        json_path = Path(args.from_json)
        if not json_path.is_file():
            print(f"错误：找不到 {json_path}", file=sys.stderr)
            return 2
        html = _render_from_json(json_path)
        print(f"（已从 {json_path} 现场渲染 {len(html):,} 字符）")
    elif args.html:
        html_path = Path(args.html)
        if not html_path.is_file():
            print(f"错误：找不到 {html_path}", file=sys.stderr)
            return 2
        html = html_path.read_text(encoding="utf-8")
    else:
        parser.print_help()
        return 2

    _passed, failed = check_html(html)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
