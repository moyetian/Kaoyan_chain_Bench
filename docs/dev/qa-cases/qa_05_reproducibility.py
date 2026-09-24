#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qa_05_reproducibility.py —— 可复现性实测（gen_fixtures / 评分两次一致 / 固定 seed run_id 稳定）。

    cd /tmp/kb_lab && python3 /workspace/KaoyanBench/docs/dev/qa-cases/qa_05_reproducibility.py
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

PY = __import__("sys").executable


def sh(cmd, timeout=300):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def line(t):
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)


def agg_sha():
    """自行计算 benchmark 下所有 fixture 文件的聚合 sha256（不信任任何人的输出）。"""
    exts = ("*.csv", "*.txt", "*.html", "*.pdf", "manifest.json")
    files = []
    for root in ("benchmark/fixtures", "benchmark/tasks"):
        for e in exts:
            files += list(Path(root).rglob(e))
    files = sorted(set(files))
    h = hashlib.sha256()
    for f in files:
        h.update(str(f.relative_to(".")).encode())
        h.update(f.read_bytes())
    return h.hexdigest(), len(files)


def t_gen_fixtures():
    line("[D-gen_fixtures 两次聚合 sha256 一致]")
    sh("python3 tools/gen_fixtures.py")
    h1, n1 = agg_sha()
    print(f"  #1: {h1}  ({n1} 个文件)")
    sh("python3 tools/gen_fixtures.py")
    h2, n2 = agg_sha()
    print(f"  #2: {h2}  ({n2} 个文件)")
    print(f"  [{'PASS' if h1 == h2 else 'FAIL'}] 两次聚合 sha256 一致")


def t_scoring_twice():
    line("[D-同任务+同答案评分两次逐字段一致]")
    from kaoyanbench.core.config import load_config, load_source_levels
    from kaoyanbench.core.registry import load_task
    from kaoyanbench.core.models import AgentOutput
    from kaoyanbench.core.grader import get_grader, GradeContext
    from pathlib import Path as P

    cfg = load_config(root=".")
    task = load_task("benchmark/tasks/public/exam/EXAM-001")
    out = AgentOutput(final_answer='{"count": 10, "subject": "数据结构"}')

    def once():
        ctx = GradeContext(task_dir=P("benchmark/tasks/public/exam/EXAM-001"),
                           workspace_dir=P("/tmp/qa_ws"), snapshot_dir=None,
                           offline_replay=False, semantic_client=None,
                           source_levels=load_source_levels(cfg),
                           weights=cfg.evaluation.weights, now="2026-01-01T00:00:00+00:00")
        return get_grader(task).grade(task, out, ctx).to_dict()

    d1, d2 = once(), once()
    for d in (d1, d2):
        d.pop("graded_at", None)
    same = d1 == d2
    print(f"  [{'PASS' if same else 'FAIL'}] 两次评分逐字段一致（含 checks.passed / detail）")
    if not same:
        for k in d1:
            if d1[k] != d2[k]:
                print(f"    DIFF {k}: {d1[k]} vs {d2[k]}")
    print(f"  score={d1['score']['total']}  checks={[(c['id'], c['passed']) for c in d1['checks']]}")


def t_seed_stability():
    line("[D-固定 seed -> run_id 稳定]")
    shutil.rmtree("results", ignore_errors=True)
    sh("kyb --root . run --suite smoke --agent mock --tag _qa_seed --seed 42")
    a = sorted(p.name for p in Path("results/runs").glob("*.jsonl"))
    shutil.rmtree("results", ignore_errors=True)
    sh("kyb --root . run --suite smoke --agent mock --tag _qa_seed --seed 42")
    b = sorted(p.name for p in Path("results/runs").glob("*.jsonl"))
    print(f"  #1: {a}")
    print(f"  #2: {b}")
    print(f"  [{'PASS' if a == b else 'FAIL'}] 两次 run_id 完全一致")


if __name__ == "__main__":
    t_gen_fixtures()
    t_scoring_twice()
    t_seed_stability()
    line("可复现性实测完成")
