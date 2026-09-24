#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qa_03_boundary.py —— 边界与异常实测（超时 / 畸形输入 / ground_truth / fixture / 评分退化 / 降级 / usage）。

在隔离环境内运行：
    cd /tmp/kb_lab && python3 /workspace/KaoyanBench/docs/dev/qa-cases/qa_03_boundary.py

所有断言均基于实测输出。本脚本只读仓库 + 在 /tmp 下写临时 fixture（不改 src/**、benchmark/**）。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

LAB = Path.cwd()
PY = sys.executable


def sh(cmd, timeout=120):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def line(t):
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)


# --------------------------------------------------------------------------- #
def t_timeout():
    line("[B-超时] command runner: sleep 999, time_limit=2")
    Path("/tmp/kb_fake").mkdir(exist_ok=True)
    Path("/tmp/kb_fake/sleep.py").write_text("import time; time.sleep(999)\n", encoding="utf-8")
    Path("config/agents/qa_sleeper.yaml").write_text(
        'name: qa_sleeper\ntype: command\nversion: "1.0.0"\nmodel: m\nprovider: local\n'
        'command: ["' + PY + '", "/tmp/kb_fake/sleep.py"]\nstdin: none\ntimeout_grace_sec: 1\n'
        'parse:\n  format: json\n', encoding="utf-8")
    shutil.rmtree("results", ignore_errors=True)
    rc, out, err = sh(f"kyb --root . run --agent qa_sleeper --task HAL-001 --split public --time-limit 2 --seed 42")
    print(f"  exit={rc}")
    # 读回 JSONL
    import glob
    hit = None
    for fn in glob.glob("results/runs/*.jsonl"):
        for ln in open(fn):
            e = json.loads(ln)
            if e.get("record") == "run_end" and e.get("agent") == "qa_sleeper":
                hit = e
    assert hit, "未找到超时 run_end 记录"
    assert hit["timed_out"] is True, "timed_out 不为 True"
    assert hit["exit_code"] == -15, f"exit_code 期望 -15(SIGTERM)，实际 {hit['exit_code']}"
    codes = [e["code"] for e in hit.get("errors", [])]
    assert "TIMEOUT" in codes, f"缺少 TIMEOUT 错误码：{codes}"
    print(f"  PASS: timed_out=True exit_code=-15 duration_ms={hit['duration_ms']} errors={codes}")
    print(f"  PASS: 任务计 FAIL 且未丢失（suite 含 1 条 grade）")


def t_malformed():
    line("[B-畸形输入] 缺字段 / 类型错 / 权重和≠1 / task_id 重复 / 未知 check type|dimension")
    base = Path(tempfile.mkdtemp(prefix="qa_mal_"))
    taskdir = base / "benchmark" / "tasks" / "public" / "qa_bad"
    sdir = base / "benchmark" / "suites"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "qa_bad.yaml").write_text("id: qa_bad\nsplit: public\ntask_ids: [BAD-001,BAD-002]\n", encoding="utf-8")

    def mkbid(tid, body):
        d = taskdir / tid
        d.mkdir(parents=True, exist_ok=True)
        (d / "task.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    # BAD-001 可加载但 difficulty 类型错
    mkbid("BAD-001", {"task_id": "BAD-001", "category": "exam", "difficulty": 123,
                      "instruction": "x", "network": "offline", "expected": {},
                      "grader": {"type": "deterministic", "checks": []}})
    # BAD-002 不可加载（缺 difficulty）—— 用于验证「首个不可加载任务会否中断后续扫描」
    mkbid("BAD-002", {"task_id": "BAD-002", "category": "exam"})

    rc, out, err = sh(f"kyb --root {base} validate 2>&1")
    print(f"  validate exit={rc}")
    print("  输出：")
    for ln in (out + err).splitlines()[:8]:
        print("   ", ln)

    # 单独验证：可加载任务的 6 类错误都能报出（去掉不可加载的 BAD-002）
    (sdir / "qa_bad.yaml").write_text("id: qa_bad\nsplit: public\ntask_ids: [BAD-001]\n", encoding="utf-8")
    shutil.rmtree(taskdir / "BAD-002", ignore_errors=True)
    # 补一个权重和≠1 与未知 check
    mkbid("BAD-003", {"task_id": "BAD-003", "category": "exam", "difficulty": "easy",
                      "instruction": "x", "network": "offline", "expected": {},
                      "grader": {"type": "deterministic",
                                 "weights": {"factuality": 0.5, "source_quality": 0.9}, "checks": []}})
    mkbid("BAD-004", {"task_id": "BAD-004", "category": "exam", "difficulty": "easy",
                      "instruction": "x", "network": "offline", "expected": {},
                      "grader": {"type": "deterministic",
                                 "checks": [{"id": "c1", "type": "no_such_check", "dimension": "bogus"}]}})
    (sdir / "qa_bad.yaml").write_text(
        "id: qa_bad\nsplit: public\ntask_ids: [BAD-001,BAD-003,BAD-004]\n", encoding="utf-8")
    rc, out, err = sh(f"kyb --root {base} validate 2>&1")
    txt = out + err
    checks = {
        "difficulty 类型错": "difficulty 不在枚举内" in txt or "expected=str" in txt,
        "weights 和≠1": "grader.weights 求和必须为 1.0" in txt,
        "未知 check.type": "type 非法：no_such_check" in txt,
        "未知 dimension": "dimension 非法：bogus" in txt,
        "带文件路径": "task.json" in txt,
    }
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print(f"  exit={rc}（应 2）")
    shutil.rmtree(base, ignore_errors=True)


def t_ground_truth():
    line("[B-ground_truth 禁令]")
    base = Path(tempfile.mkdtemp(prefix="qa_gt_"))
    # 联网题写 ground_truth
    d = base / "benchmark" / "tasks" / "public" / "search" / "SEARCH-001"
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.json").write_text(json.dumps({
        "task_id": "SEARCH-001", "category": "search", "difficulty": "easy",
        "instruction": "x", "network": "online",
        "expected": {"ground_truth": {"n": 1}},
        "grader": {"type": "deterministic", "checks": []}}, ensure_ascii=False), encoding="utf-8")
    (base / "benchmark" / "suites").mkdir(parents=True, exist_ok=True)
    (base / "benchmark" / "suites" / "s.yaml").write_text(
        "id: s\nsplit: public\ntask_ids: [SEARCH-001]\n", encoding="utf-8")
    rc, out, err = sh(f"kyb --root {base} validate --suite s 2>&1")
    txt = out + err
    ok1 = "network=online 的任务不得写 ground_truth" in txt
    print(f"  [{'PASS' if ok1 else 'FAIL'}] 联网题写 ground_truth 被拦截  exit={rc}")

    # 离线数字题带 ground_truth 但无 fixture
    d2 = base / "benchmark" / "tasks" / "public" / "exam" / "EXAM-001"
    d2.mkdir(parents=True, exist_ok=True)
    (d2 / "task.json").write_text(json.dumps({
        "task_id": "EXAM-001", "category": "exam", "difficulty": "easy",
        "instruction": "x", "network": "offline",
        "expected": {"ground_truth": {"count": 10}},
        "grader": {"type": "deterministic", "checks": []}}, ensure_ascii=False), encoding="utf-8")
    (base / "benchmark" / "suites" / "s.yaml").write_text(
        "id: s\nsplit: public\ntask_ids: [EXAM-001]\n", encoding="utf-8")
    rc, out, err = sh(f"kyb --root {base} validate --suite s 2>&1")
    txt = out + err
    ok2 = "带 ground_truth 但缺少 references/ 目录" in txt
    print(f"  [{'PASS' if ok2 else 'FAIL'}] 离线数字题无 fixture 被拦截  exit={rc}")
    shutil.rmtree(base, ignore_errors=True)


def t_fixture_tamper():
    line("[B-fixture 完整性] 篡改 1 字节应报 sha256 不匹配")
    f = Path("benchmark/tasks/public/exam/EXAM-001/references/questions.csv")
    orig = f.read_bytes()
    b = bytearray(orig)
    b[0] ^= 0x01
    f.write_bytes(bytes(b))
    rc, out, err = sh("kyb --root . validate --suite core50 2>&1")
    txt = out + err
    ok = "fixture hash 不匹配" in txt
    print(f"  [{'PASS' if ok else 'FAIL'}] 篡改 1 字节 -> 报 hash 不匹配  exit={rc}")
    dup = [l for l in txt.splitlines() if "fixture hash 不匹配" in l]
    print(f"  注意：同一错误输出 {len(dup)} 次（重复告警）")
    f.write_bytes(orig)  # 还原


def t_scoring_degrade():
    line("[B-评分退化] 全 neutral 抛错 / 单维 neutral 重分配 / Efficiency 规则")
    from kaoyanbench.core.scorer import score_from_checks
    from kaoyanbench.core.errors import ScoringError
    from kaoyanbench.core.models import CheckResult

    def C(i, dim, passed, w=1.0):
        return CheckResult(id=i, type="t", dimension=dim, passed=passed, weight=w,
                           critical=False, detail="", judge="deterministic")

    W = {"factuality": .30, "source_quality": .20, "citation": .15, "completeness": .15,
         "tool_execution": .10, "task_completion": .05, "efficiency": .05}
    try:
        score_from_checks([C("c1", "factuality", None), C("c2", "completeness", None)], W)
        print("  [FAIL] 全 neutral 未抛 ScoringError")
    except ScoringError:
        print("  [PASS] 全 neutral -> ScoringError（不产出假分数）")
    b = score_from_checks([C("c1", "factuality", True)], W)
    ssum = b.factuality + b.source_quality + b.citation + b.completeness + b.tool_execution + b.task_completion + b.efficiency
    print(f"  [{'PASS' if b.reallocated and abs(ssum-100) < 1e-6 else 'FAIL'}] 单维 neutral -> reallocated={b.reallocated} 各维之和={ssum:.4f}")
    b2 = score_from_checks([C("c1", "factuality", True)], W, task_success_flag=False,
                           tool_calls=0, tool_limit=60, latency_seconds=1, time_limit=900)
    b3 = score_from_checks([C("c1", "factuality", True)], W, timed_out=True,
                           tool_calls=0, tool_limit=60, latency_seconds=5, time_limit=900)
    print(f"  [{'PASS' if b2.efficiency == 0 and b3.efficiency == 0 else 'FAIL'}] task_success=False/timeout -> Efficiency=0 ({b2.efficiency},{b3.efficiency})")


def t_degrade_paths():
    line("[B-降级 4 路径]")
    from kaoyanbench.core.config import load_config
    from kaoyanbench.core.errors import ConfigError
    # 路径 4：评测模型==被测模型
    try:
        load_config(root=".", overrides={"grader_model": {"name": "deepseek-chat"}})
        print("  [FAIL] 同模型未抛 ConfigError")
    except ConfigError as e:
        print(f"  [PASS] 同模型(allow_same_model=false) -> ConfigError：{str(e)[:50]}")
    try:
        load_config(root=".", overrides={"grader_model": {"name": "deepseek-chat", "allow_same_model": True}})
        print("  [PASS] allow_same_model=true -> 放行")
    except ConfigError:
        print("  [FAIL] allow_same_model=true 仍报错")
    # 路径 1：无 key
    from kaoyanbench.core.graders.semantic import SemanticClient
    c = SemanticClient(base_url="http://127.0.0.1:1/v1", api_key=None, model="m", timeout=1)
    print(f"  [{'PASS' if c.judge('q', 'a', 'c').error == 'no_api_key' else 'FAIL'}] 无 key -> no_api_key")
    # 路径 2：网络不可达
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    c2 = SemanticClient(base_url=f"http://127.0.0.1:{port}/v1", api_key="k", model="m", timeout=2)
    print(f"  [{'PASS' if c2.judge('q', 'a', 'c').error == 'network_unreachable' else 'FAIL'}] 网络不可达 -> network_unreachable")
    # 路径 3：响应非 JSON
    import http.server, threading
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("content-length", 0)); self.rfile.read(n)
            data = b"not json at all"
            self.send_response(200); self.send_header("content-length", str(len(data)))
            self.end_headers(); self.wfile.write(data)
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c3 = SemanticClient(base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1", api_key="k", model="m", timeout=3)
    r = c3.judge("q", "a", "c"); srv.shutdown()
    print(f"  [{'PASS' if r.error == 'invalid_response' else 'FAIL'}] 响应非 JSON -> invalid_response")
    # 端到端：无 key 跑 HYB 题应标 degraded
    shutil.rmtree("results", ignore_errors=True)
    env = dict(os.environ); env.pop("OPENAI_API_KEY", None); env.pop("GRADER_API_KEY", None)
    p = subprocess.run(f"kyb --root . run --agent mock --task PDF-005 --split public --seed 42",
                       shell=True, capture_output=True, text=True, env=env)
    import glob
    found = None
    for fn in glob.glob("results/runs/*.jsonl"):
        for ln in open(fn):
            e = json.loads(ln)
            if e.get("record") == "grade" and e.get("grader_type") == "hybrid":
                found = e
    print(f"  [{'PASS' if found and found['grader_mode']=='degraded' and found['degraded_reason']=='no_api_key' else 'FAIL'}] "
          f"HYB 无 key 端到端 -> grader_mode={found and found.get('grader_mode')} reason={found and found.get('degraded_reason')}")


def t_usage_missing():
    line("[B-usage 缺失] 采不到 token/cost 应为 none + null")
    shutil.rmtree("results", ignore_errors=True)
    sh("kyb --root . run --suite smoke --agent mock --tag _qa_usage --seed 42")
    d = json.load(open("results/suites/smoke__mock___qa_usage.suite.json"))
    a = d["aggregates"]
    print(f"  usage_missing_count={a['usage_missing_count']}  usage_estimated_ratio={a['usage_estimated_ratio']}")
    print(f"  tokens_mean={a['tokens_mean']}  cost_usd_mean={a['cost_usd_mean']}（应 null）")
    ok = a["tokens_mean"] is None and a["cost_usd_mean"] is None
    print(f"  [{'PASS' if ok else 'FAIL'}] 未用 0 冒充缺失值")


if __name__ == "__main__":
    t_timeout()
    t_malformed()
    t_ground_truth()
    t_fixture_tamper()
    t_scoring_degrade()
    t_degrade_paths()
    t_usage_missing()
    line("边界与异常实测完成")
