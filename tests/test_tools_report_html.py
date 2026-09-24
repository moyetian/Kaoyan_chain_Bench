"""`tools/check_report_html.py` 的回归测试。

重点覆盖 2026-09-23 的修复：
- URL 扫描需排除 ``<script>`` 数据块——内嵌 JSON 里的来源 URL（如
  ``https://yz.chsi.com.cn/tj/``）是报告内容，不是外部资源引用，
  此前会被误判为「外部地址」导致 26/27 FAIL；
- 主动外联改由独立断言覆盖（fetch/XHR/WebSocket 等）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_tool():
    path = REPO_ROOT / "tools" / "check_report_html.py"
    spec = importlib.util.spec_from_file_location("check_report_html_tool", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_report_html_tool"] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def _results(html: str) -> dict[str, bool]:
    return {name: ok for ok, name, _ in tool._check_no_external(html)}


def test_strip_script_contents_removes_data_but_keeps_tags() -> None:
    html = '<p>hi</p><script>var d = {"u": "https://x.example/a"};</script>'
    out = tool._strip_script_contents(html)
    assert "https://x.example/a" not in out
    assert "<script>" in out and "</script>" in out
    assert "<p>hi</p>" in out


def test_data_urls_inside_script_do_not_fail_url_check() -> None:
    """报告数据里的来源 URL 不得被判为外部资源。"""
    html = (
        "<html><head><style>.a{color:red}</style></head><body>"
        '<script>var d = {"detail": "https://yz.chsi.com.cn/tj/"};</script>'
        "</body></html>"
    )
    assert _results(html)["http(s)://"] is True


def test_svg_namespace_still_allowed() -> None:
    html = '<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    assert _results(html)["http(s)://"] is True


def test_real_external_reference_still_caught() -> None:
    """剥离 script 后，真实的外部资源引用仍必须被捕获。"""
    html = '<link rel="stylesheet" href="https://cdn.example/a.css">'
    assert _results(html)["<link"] is False
    assert _results(html)["http(s)://"] is False


def test_active_network_api_detected() -> None:
    ok, name, detail = tool._check_active_network('<script>fetch("https://x.example/a");</script>')
    assert ok is False
    assert "fetch(" in detail


def test_active_network_api_absent() -> None:
    ok, _, detail = tool._check_active_network("<script>document.title = 'x';</script>")
    assert ok is True
    assert "未出现" in detail
