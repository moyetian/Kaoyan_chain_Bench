#!/usr/bin/env python3
"""qa_09_cheat_echo_probe.py —— 「抄题面」作弊 Runner 探针（P1-2 更强判据）

背景
----
首轮 QA 发现 core50 可被 `echo` Runner（回显 instruction）刷到 28%，判为 P1-2。
任务集工程师整改后声称 echo→0%、anti-echo warn 84→0。
本脚本**不复用开发者的口径**，自行构造一个比 echo 更强的"作弊 Agent"来探基准可判别性：

作弊策略（"抄题面"）：
  读取 task.json，把 instruction + expected（含 must_find/any_of）+ grader.checks
  的 value/patterns/any_of/values 等**判定词**全部原文拼接，作为 final_answer 输出。
  即：把"题面泄漏 + 判定词泄漏"叠加到极致，若仍能拿高分 → 泄漏仍存在。

实现方式：
  - 用 `command` 型 Runner，command 调用 `qa_08_cheat_runner.sh {task_dir}`；
  - 以 `mini_yaml` 之外的副本 agent 配置（写在工作目录 config/agents/qa_cheat.yaml）注册；
  - 跑 core50，**自行读取 suite 结果 JSON 的 grades[].metrics.task_success** 计算通过率，
    不看打印的 summary 行。

用法：
  python3 docs/dev/qa-cases/qa_09_cheat_echo_probe.py [--lab /tmp/kb_lab]

注意：脚本只读仓库源码，全部产物落在 --lab 隔离目录；不修改 src/**、cli.py、
benchmark/**、config/**（仓库根）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # /workspace/KaoyanBench
CHEAT_SH = REPO / "docs/dev/qa-cases/qa_08_cheat_runner.sh"


def sh(cmd: str, cwd: Path, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, check=check)


def setup_lab(lab: Path) -> None:
    if lab.exists():
        shutil.rmtree(lab)
    shutil.copytree(REPO, lab)
    for junk in ("reports", "results", ".pytest_cache"):
        shutil.rmtree(lab / junk, ignore_errors=True)


def register_cheat_agent(lab: Path) -> None:
    """在隔离 lab 的 config/agents 下登记作弊 agent（不触碰仓库 config）。"""
    (lab / "config/agents/qa_cheat.yaml").write_text(
        "name: qa_cheat\n"
        "type: command\n"
        'version: "1.0.0"\n'
        f'command: "bash {CHEAT_SH} {{task_dir}}"\n'
        "stdin: none\n"
        "parse:\n"
        "  format: json\n"
        "  answer_field: answer\n"
        "params: {}\n",
        encoding="utf-8",
    )


def run_suite(lab: Path, agent: str, suite: str, tag: str) -> Path:
    sh(f"kyb --root . run --suite {suite} --agent {agent} --tag {tag} --seed 42", lab)
    suites = sorted((lab / "results/suites").glob(f"{suite}__{agent}__{tag}.suite.json"))
    if not suites:
        raise SystemExit(f"未找到 {suite}__{agent}__{tag}.suite.json")
    return suites[-1]


def pass_rate(suite_json: Path) -> dict:
    """**自行计算**通过率：从 grades[].metrics.task_success 读，不用 summary。"""
    d = json.loads(suite_json.read_text(encoding="utf-8"))
    grades = d["grades"]
    passed = [g["task_id"] for g in grades if g.get("metrics", {}).get("task_success")]
    scores = [g["score"]["total"] for g in grades]
    return {
        "n": len(grades),
        "passed": len(passed),
        "rate_pct": 100.0 * len(passed) / len(grades) if grades else 0.0,
        "passed_ids": sorted(passed),
        "score_mean": sum(scores) / len(scores) if scores else 0.0,
        "score_max": max(scores) if scores else 0.0,
        "summary_reported": d["aggregates"].get("task_success_rate"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", default="/tmp/kb_lab")
    ap.add_argument("--suite", default="core50")
    args = ap.parse_args()
    lab = Path(args.lab)

    print("=" * 72)
    print("qa_09  「抄题面」作弊 Runner 探针")
    print("=" * 72)
    print(f"仓库: {REPO}")
    print(f"隔离 lab: {lab}")
    print(f"作弊脚本: {CHEAT_SH}")

    setup_lab(lab)
    register_cheat_agent(lab)

    results = {}
    for agent, tag in (("echo", "_qa_echo"), ("qa_cheat", "_qa_cheat"), ("mock", "_qa_mock")):
        sj = run_suite(lab, agent, args.suite, tag)
        results[agent] = pass_rate(sj)
        r = results[agent]
        print()
        print(f"### {agent} @ {args.suite}")
        print(f"  MY CALC : passed={r['passed']}/{r['n']}  rate={r['rate_pct']:.1f}%  "
              f"score_mean={r['score_mean']:.2f}  score_max={r['score_max']:.2f}")
        print(f"  SUMMARY : task_success_rate={r['summary_reported']}")
        print(f"  passed  : {r['passed_ids']}")

    print()
    print("=" * 72)
    print("结论")
    print("=" * 72)
    print(f"  echo     通过率 = {results['echo']['rate_pct']:.1f}%")
    print(f"  抄题面    通过率 = {results['qa_cheat']['rate_pct']:.1f}%   ← 比 echo 更强的作弊")
    print(f"  mock     通过率 = {results['mock']['rate_pct']:.1f}%   （参考答案应 100%，防误伤）")
    cheat_pass = results["qa_cheat"]["passed"]
    if cheat_pass == 0:
        print("  ✅ 抄题面策略无法得分：题面→判定词的泄漏已被消除")
    else:
        print(f"  ❌ 抄题面仍能通过 {cheat_pass} 题：泄漏仍存在，见 passed_ids")
    return 0


if __name__ == "__main__":
    sys.exit(main())
