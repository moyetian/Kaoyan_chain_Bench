#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qa_04_security.py —— 越权与安全实测（env 白名单 / 路径穿越 / 并发隔离 / HTML 零外部请求 / 报告一致性）。

    cd /tmp/kb_lab && python3 /workspace/KaoyanBench/docs/dev/qa-cases/qa_04_security.py
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

PY = sys.executable


def sh(cmd, timeout=180, env=None):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, env=env)
    return p.returncode, p.stdout, p.stderr


def line(t):
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)


def t_env_whitelist():
    line("[C-env 白名单] env_passthrough: [] 时子进程看不到 OPENAI_API_KEY")
    Path("/tmp/kb_fake").mkdir(exist_ok=True)
    Path("/tmp/kb_fake/leak.py").write_text(
        "import os, json\n"
        "leak={k:v for k,v in os.environ.items() if 'API_KEY' in k or 'SECRET' in k}\n"
        "print(json.dumps({'final_answer':'x','leaked':leak,'keys':sorted(os.environ)}))\n",
        encoding="utf-8")
    Path("config/agents/qa_env.yaml").write_text(
        'name: qa_env\ntype: command\nversion: "1.0.0"\nmodel: m\nprovider: local\n'
        f'command: ["{PY}", "/tmp/kb_fake/leak.py"]\nstdin: none\nenv_passthrough: []\n'
        'parse:\n  format: json\n  answer_field: final_answer\n', encoding="utf-8")
    env = dict(os.environ); env["OPENAI_API_KEY"] = "sk-LEAK-CANARY-42"
    shutil.rmtree("results", ignore_errors=True)
    sh("kyb --root . run --agent qa_env --task HAL-001 --split public --seed 1", env=env)
    f = glob.glob("results/workspaces/outputs/*.stdout.txt")
    d = json.load(open(f[0]))
    ok = "OPENAI_API_KEY" not in d["keys"] and not d["leaked"]
    print(f"  [{'PASS' if ok else 'FAIL'}] 白名单 [] -> 子进程 env 无 API_KEY（keys={len(d['keys'])} leak={d['leaked']}）")

    # 正对照：显式放行
    Path("config/agents/qa_env2.yaml").write_text(
        'name: qa_env2\ntype: command\nversion: "1.0.0"\nmodel: m\nprovider: local\n'
        f'command: ["{PY}", "/tmp/kb_fake/leak.py"]\nstdin: none\nenv_passthrough: ["OPENAI_API_KEY"]\n'
        'parse:\n  format: json\n  answer_field: final_answer\n', encoding="utf-8")
    sh("kyb --root . run --agent qa_env2 --task HAL-001 --split public --seed 2", env=env)
    f2 = sorted(glob.glob("results/workspaces/outputs/*.stdout.txt"))[-1]
    d2 = json.load(open(f2))
    print(f"  [{'PASS' if d2['leaked'] else 'FAIL'}] 显式放行 -> 子进程可见（leaked={list(d2['leaked'])}）")


def t_path_traversal():
    line("[C-路径穿越] manifest 写 ../../etc/passwd 必须被拒")
    import hashlib, tempfile
    base = Path(tempfile.mkdtemp(prefix="qa_trav_"))
    d = base / "benchmark" / "tasks" / "public" / "exam" / "EXAM-001" / "references"
    d.mkdir(parents=True, exist_ok=True)
    (d / "q.csv").write_text("year,chapter\n2015,OS\n", encoding="utf-8")
    raw = Path("/etc/passwd").read_bytes()
    (d / "manifest.json").write_text(json.dumps({
        "files": {
            "q.csv": {"sha256": hashlib.sha256((d / "q.csv").read_bytes()).hexdigest(),
                      "size": (d / "q.csv").stat().st_size},
            "../../../../etc/passwd": {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}}}),
        encoding="utf-8")
    # 必备的合法 task.json（否则 validate 会先因其它原因失败，掩盖穿越检查）
    (base / "benchmark" / "tasks" / "public" / "exam" / "EXAM-001" / "task.json").write_text(json.dumps({
        "task_id": "EXAM-001", "category": "exam", "difficulty": "easy", "instruction": "x",
        "network": "offline", "expected": {"ground_truth": {"n": 1}},
        "grader": {"type": "deterministic",
                   "checks": [{"id": "c1", "type": "numeric", "dimension": "factuality",
                               "path": "$.n", "op": "eq", "value": 1}]}}, ensure_ascii=False),
        encoding="utf-8")
    (base / "benchmark" / "suites").mkdir(parents=True, exist_ok=True)
    (base / "benchmark" / "suites" / "s.yaml").write_text(
        "id: s\nsplit: public\ntask_ids: [EXAM-001]\n", encoding="utf-8")
    rc, out, err = sh(f"kyb --root {base} validate --suite s 2>&1")
    txt = out + err
    ok = "文件名非法" in txt and ".." in txt
    print(f"  [{'PASS' if ok else 'FAIL'}] 路径穿越被拒  exit={rc}")
    print(f"    {[l.strip() for l in txt.splitlines() if '非法' in l][:1]}")
    shutil.rmtree(base, ignore_errors=True)


def t_concurrency_isolation():
    line("[C-并发隔离] --concurrency 3 每 run 独立 workspace")
    shutil.rmtree("results", ignore_errors=True)
    sh("kyb --root . run --suite smoke --agent mock --tag _qa_conc --seed 42 --concurrency 3 --keep-workspace")
    ws = [p for p in Path("results/workspaces").glob("run_*")]
    print(f"  [{'PASS' if len(ws) == 6 and len(set(ws)) == 6 else 'FAIL'}] 6 个任务 -> {len(ws)} 个唯一 workspace")
    # 每个 workspace 有独立 references/home/tmp
    indep = all((w / "home").exists() for w in ws)
    print(f"  [{'PASS' if indep else 'FAIL'}] 每个 workspace 有独立 HOME 重定向")


def t_html():
    line("[C-HTML 零外部请求] report.html 无外链资源")
    # 生成报告
    sh("kyb --root . report --suite smoke --agent mock --tag _qa_conc --format json,markdown,html,csv")
    h = Path("reports/smoke__mock___qa_conc/report.html").read_text(encoding="utf-8")
    import re
    urls = [u for u in re.findall(r"https?://[^\s\"'<>)]+", h) if "www.w3.org/2000/svg" not in u]
    has_link = "<link" in h
    has_src = 'src="' in h
    has_fetch = "fetch(" in h or "XMLHttpRequest" in h
    print(f"  [{'PASS' if not urls else 'FAIL'}] 非 SVG 命名空间的 http(s) 引用：{urls[:3] or '无'}")
    print(f"  [{'PASS' if not has_link else 'FAIL'}] 无 <link>；[{'PASS' if not has_src else 'FAIL'}] 无 src=；"
          f"[{'PASS' if not has_fetch else 'FAIL'}] 无 fetch/XHR")

    # 标准库 html.parser 校验标签闭合
    class P(HTMLParser):
        void = {"meta", "link", "br", "hr", "img", "input", "area", "base", "col", "embed", "source", "track", "wbr"}
        def __init__(self):
            super().__init__(); self.stack = []; self.bad = []
        def handle_starttag(self, tag, attrs):
            if tag not in self.void:
                self.stack.append(tag)
        def handle_endtag(self, tag):
            if tag in self.void:
                return
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()
            else:
                self.bad.append(tag)
    p = P(); p.feed(h)
    ok = not p.stack and not p.bad
    print(f"  [{'PASS' if ok else 'FAIL'}] html.parser 标签闭合：未闭合={p.stack} 错配={p.bad}")


def t_report_consistency():
    line("[C-报告数据一致性]")
    d = json.load(open("reports/smoke__mock___qa_conc/report.json"))
    print(f"  [{'PASS' if len(d['tasks']) == d['suite']['task_count'] else 'FAIL'}] "
          f"tasks 长度 {len(d['tasks'])} == suite.task_count {d['suite']['task_count']}")
    print(f"  [{'PASS' if len(d['errors']) == 11 else 'FAIL'}] errors 11 类：{len(d['errors'])}")
    enum = ["search", "university", "policy", "exam", "pdf", "planning", "research", "hallucination"]
    present = [c["category"] for c in d["by_category"]]
    ordered = present == [c for c in enum if c in present]
    print(f"  [{'PASS' if ordered else 'FAIL'}] by_category 顺序为枚举序：{present}")
    unk = [t["task_id"] for t in d["tasks"] if "unknown" in (t["category"], t["difficulty"], t["network"])]
    print(f"  [{'PASS' if not unk else 'FAIL'}] 明细无 unknown：{unk}")


if __name__ == "__main__":
    t_env_whitelist()
    t_path_traversal()
    t_concurrency_isolation()
    t_html()
    t_report_consistency()
    line("越权与安全实测完成")
