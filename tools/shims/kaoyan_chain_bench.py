#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kaoyan_chain_bench.py —— KaoyanBench ↔ 考研学习链的评测适配器（shim）。

背景
----
考研学习链（`ky`）是交互式备考系统，没有 `kaoyan run --prompt-file … --json`
这类单发评测契约。本 shim 把它内部的 `AgentRunner`（tools/agent/loop.py）
以 headless 方式驱动一轮，输出 KaoyanBench `ParseSpec(json)` 可解析的结果。

关键设计（诚实性优先）
----------------------
1. 密钥不出被测仓库：LLM Key 只从 `KAOYAN_CHAIN_CONFIG` 指向的 ky_config.json
   在运行时读取；本文件与 bench 仓库绝不落地任何密钥。
2. 不伪造轨迹：`tool_calls` 来自对 `ToolRegistry.execute_tool` 的包装记录；
   无调用即 `[]`（bench 会按 neutral 重分配，不加分也不扣分）。
   `sources` 只从最终答案中用正则提取真实出现的 URL，不编造。
   `usage` 上报 `none`（bench 禁止填 0，`none` 即“未知”）。
3. 隔离：被测 Agent 的 `workspace_root` 直接指向 bench 分配的 workspace，
   其记忆/state 文件不出该目录；`force_allow_all` 仅作用于本次评测进程，
   不修改被测仓库任何文件。
4. 确定性：覆盖 `temperature=0.0`（评测可复现；被测仓库默认 0.3 保持不动）。

用法（由 config/agents/kaoyan_chain_shim.yaml 调用，勿手写路径）
----
    py tools/shims/kaoyan_chain_bench.py \
      --prompt-file <workspace>/prompt.txt \
      --workspace <workspace> \
      --answer-file <workspace>/answer.json \
      --usage-file <workspace>/usage.json \
      --task-id SEARCH-001 [--max-steps 8]

环境变量：KAOYAN_CHAIN_HOME（被测仓库根）、KAOYAN_CHAIN_CONFIG（ky_config.json）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s\"'<>（）()【】\[\]]+")


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="kaoyan_chain 评测适配器")
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--answer-file", required=True)
    parser.add_argument("--usage-file", required=True)
    parser.add_argument("--task-id", default="")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--request-timeout", type=float, default=60.0)
    return parser.parse_args(argv)


def _extract_sources(final_answer: str, limit: int = 20) -> list[dict]:
    seen: list[str] = []
    for match in _URL_RE.findall(final_answer or ""):
        url = match.rstrip(".,;:!?，。；：！？")
        if url not in seen:
            seen.append(url)
        if len(seen) >= limit:
            break
    sources = []
    for url in seen:
        try:
            domain = urlparse(url).hostname or ""
        except ValueError:
            domain = ""
        sources.append({"url": url, "domain": domain, "title": ""})
    return sources


def _isolate_home(workspace: Path) -> None:
    """把用户主目录重定向进 bench workspace（P0 隔离修复）。

    背景：bench 沙箱为隔离会剥离 USERPROFILE，被测仓库 MemoryManager 在
    __init__ 中无条件调用 Path.home()（~/.ky 全局记忆），缺失即崩溃
    （RuntimeError: Could not determine home directory）。
    重定向后还有两个好处：① 评测读写的是隔离记忆，不会污染用户真实的
    ~/.ky 学习数据；② 不读用户真实记忆，评测更干净（无个性化串扰）。
    必须在 import 被测仓库模块之前执行（其模块级代码也可能读 home）。
    """
    home = workspace / "home"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["USERPROFILE"] = str(home)
    os.environ["HOME"] = str(home)


def main(argv=None) -> int:
    args = _parse_args(argv)
    started = time.time()

    home = os.environ.get("KAOYAN_CHAIN_HOME", "").strip()
    config_path = os.environ.get("KAOYAN_CHAIN_CONFIG", "").strip()
    if not home or not Path(home).is_dir():
        print(json.dumps({"final_answer": "",
                           "error": "KAOYAN_CHAIN_HOME 未设置或不是目录"},
                          ensure_ascii=False))
        return 2
    if not config_path or not Path(config_path).is_file():
        print(json.dumps({"final_answer": "",
                           "error": "KAOYAN_CHAIN_CONFIG 未设置或文件不存在"},
                          ensure_ascii=False))
        return 2

    sys.path.insert(0, home)
    sys.path.insert(0, str(Path(home) / "tools"))

    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    workspace = Path(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    _isolate_home(workspace)

    with open(config_path, encoding="utf-8") as fh:
        base_config = json.load(fh)
    config = dict(base_config)
    config["temperature"] = 0.0  # 评测可复现（不写回被测仓库配置）

    from tools.agent.loop import AgentRunner  # noqa: E402

    runner = AgentRunner(
        config,
        workspace_root=str(workspace),
        permission_mode="auto",
        max_steps=int(args.max_steps),
        quiet=True,
        request_timeout=float(args.request_timeout),
    )
    # 全自动沙箱：bench 非交互环境，沿用被测仓库自带的测试开关语义。
    runner.permissions.force_allow_all = True

    journal: list[dict] = []
    original_execute = runner.tool_registry.execute_tool

    def _logging_execute(name, tool_args, interactive=True):
        call_started = time.time()
        try:
            result = original_execute(name, tool_args, interactive=False)
            status = "ok"
        except Exception as exc:  # noqa: BLE001 - 工具异常如实记录，不中断评测
            result = f"Error: {type(exc).__name__}: {exc}"
            status = "error"
        journal.append({
            "name": str(name),
            "args": tool_args if isinstance(tool_args, dict) else {},
            "result": str(result)[:2000],
            "status": status,
            "duration_ms": int((time.time() - call_started) * 1000),
        })
        return result

    runner.tool_registry.execute_tool = _logging_execute

    bench_prompt = (
        prompt
        + "\n\n## 作答要求（评测契约，请严格遵守）\n"
        + "- 直接给出最终答案，不要反问；如需查资料请使用可用工具后再作答。\n"
        + "- 引用了网页请在答案中写出完整 URL（每条一行），未引用则不写。\n"
        + "- 若题目要求 JSON，只输出 JSON 本体：不要加解释，"
        + "不要用 ``` 代码块包裹，第一字符必须是 { 或 [。\n"
        + "- 若题目要求把文件写入 output/，请用 write_file 工具写入相对路径 "
        + "(如 output/report.json)，不要写绝对路径。\n"
    )
    try:
        final_answer = runner.run(bench_prompt, interactive=False)
    except Exception as exc:  # noqa: BLE001 - 顶层兜底：如实输出错误文本
        final_answer = f"[适配器异常] {type(exc).__name__}: {exc}"

    tool_calls = [{
        "name": entry["name"],
        "args": entry["args"],
        "result": entry["result"],
        "status": entry["status"],
        "duration_ms": entry["duration_ms"],
    } for entry in journal]
    sources = _extract_sources(str(final_answer or ""))

    payload = {
        "final_answer": str(final_answer or ""),
        "tool_calls": tool_calls,
        "sources": sources,
        "citations": [],
        "usage": {"usage_source": "none"},
    }
    text = json.dumps(payload, ensure_ascii=False)
    Path(args.answer_file).write_text(text, encoding="utf-8")
    Path(args.usage_file).write_text(
        json.dumps({"usage_source": "none"}, ensure_ascii=False), encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
