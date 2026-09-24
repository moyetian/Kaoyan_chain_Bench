#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qa_06_functional.py —— 功能正向实测（子命令 / 分布断言 / 三 Runner / Pass@k / Store / 报告四件套）。

    cd /tmp/kb_lab && python3 /workspace/KaoyanBench/docs/dev/qa-cases/qa_06_functional.py
"""
from __future__ import annotations

import collections
import json
import shutil
import subprocess
import sys
from pathlib import Path

PY = sys.executable


def sh(cmd, timeout=300):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def line(t):
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)


def t_subcommands():
    line("[A-子命令] 各子命令 --help 与基本运行")
    for sub in "list run report compare regression validate snapshot fixtures grade agent suite".split():
        rc, out, err = sh(f"kyb --root . {sub} --help")
        print(f"  [{'PASS' if rc == 0 else 'FAIL'}] {sub} --help rc={rc}")


def t_distribution():
    line("[A-分布断言] 50 题 / 类别 12-9-5-5-5-5-4-5 / 难度 10-18-15-7")
    rc, out, err = sh("kyb --root . --json list")
    d = json.loads(out)
    tasks = d["tasks"]
    cats = collections.Counter(t["category"] for t in tasks)
    diffs = collections.Counter(t["difficulty"] for t in tasks)
    order = ["search", "university", "policy", "exam", "pdf", "planning", "research", "hallucination"]
    got = [cats[c] for c in order]
    print(f"  任务数：{len(tasks)}（应 50）  [{'PASS' if len(tasks)==50 else 'FAIL'}]")
    print(f"  类别：{got}（应 [12,9,5,5,5,5,4,5]）  [{'PASS' if got==[12,9,5,5,5,5,4,5] else 'FAIL'}]")
    gd = [diffs["easy"], diffs["medium"], diffs["hard"], diffs["expert"]]
    print(f"  难度：{gd}（应 [10,18,15,7]）  [{'PASS' if gd==[10,18,15,7] else 'FAIL'}]")


def t_runners():
    line("[A-三 Runner] echo / mock / command")
    # echo
    rc, out, err = sh("kyb --root . run --agent echo --task HAL-001 --split public --seed 42")
    print(f"  [{'PASS' if rc==0 else 'FAIL'}] echo runner rc={rc}")
    # mock suite
    rc, out, err = sh("kyb --root . run --suite smoke --agent mock --tag _qa_runners --seed 42")
    print(f"  [{'PASS' if rc==0 and 'tasks=6' in out+err else 'FAIL'}] mock suite rc={rc}")
    # command (本地假 CLI)
    Path("/tmp/kb_fake").mkdir(exist_ok=True)
    Path("/tmp/kb_fake/echo_json.py").write_text(
        'import json,sys; print(json.dumps({"final_answer":"未找到官方数据"}))\n', encoding="utf-8")
    Path("config/agents/qa_cmd.yaml").write_text(
        'name: qa_cmd\ntype: command\nversion: "1.0.0"\nmodel: m\nprovider: local\n'
        f'command: ["{PY}", "/tmp/kb_fake/echo_json.py"]\nstdin: none\n'
        'parse:\n  format: json\n  answer_field: final_answer\n', encoding="utf-8")
    rc, out, err = sh("kyb --root . run --agent qa_cmd --task HAL-001 --split public --seed 42")
    print(f"  [{'PASS' if rc==0 else 'FAIL'}] command runner rc={rc}")


def t_pass_at_k():
    line("[A-Pass@1/Pass@3 + 统计量]")
    rc, out, err = sh("kyb --root . run --suite smoke --agent mock --tag _qa_pk --runs 3 --seed 42")
    d = json.load(open("results/suites/smoke__mock___qa_pk.suite.json"))
    a = d["aggregates"]
    print(f"  n_tasks={a['n_tasks']} runs_per_task={d['runs_per_task']} runs={len(d['runs'])}")
    print(f"  pass_at_1={a['pass_at_1']} pass_at_3={a['pass_at_3']} mean={a['score_mean']} median={a['score_median']} p90={a['score_p90']}")
    ok = a["n_tasks"] == 6 and len(d["runs"]) == 18
    print(f"  [{'PASS' if ok else 'FAIL'}] 6 题 × 3 次 = 18 runs，Pass@k 已计算")


def t_store():
    line("[A-Store] 幂等同步 + 删库重建一致")
    from kaoyanbench.core.store import ResultStore
    shutil.rmtree("results", ignore_errors=True)
    sh("kyb --root . run --suite smoke --agent mock --tag _qa_store --seed 42")
    s = ResultStore("results")
    c1 = s.db_counts(); s.sync_db(); c2 = s.db_counts()
    print(f"  [{'PASS' if c1==c2 else 'FAIL'}] 同步一次幂等：{c1} == {c2}")
    Path("results/benchmark.db").unlink()
    s2 = ResultStore("results"); s2.rebuild_db(); c3 = s2.db_counts()
    print(f"  [{'PASS' if c1==c3 else 'FAIL'}] 删库重建一致：{c1} == {c3}")


def t_reports():
    line("[A-报告四件套]")
    sh("kyb --root . report --suite smoke --agent mock --tag _qa_store --format json,markdown,html,csv")
    need = ["report.json", "report.md", "report.html", "report.csv"]
    got = [n for n in need if Path(f"reports/smoke__mock___qa_store/{n}").exists()]
    print(f"  [{'PASS' if len(got)==4 else 'FAIL'}] 生成 {got}")


if __name__ == "__main__":
    t_subcommands()
    t_distribution()
    t_runners()
    t_pass_at_k()
    t_store()
    t_reports()
    line("功能正向实测完成")
