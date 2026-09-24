#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qa_07_exit_codes.py —— 回归门禁与退出码实测（Δpp / strict-warn / 运行错误），并复现缺陷。

    cd /tmp/kb_lab && python3 /workspace/KaoyanBench/docs/dev/qa-cases/qa_07_exit_codes.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def sh(cmd, timeout=300):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def line(t):
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)


def setup():
    shutil.rmtree("results", ignore_errors=True)
    # baseline A（全通过）：用原 mock 答案
    Path("config/agents/qa_p.yaml").write_text(
        Path("config/agents/mock.yaml").read_text().replace("name: mock", "name: qa_p", 1),
        encoding="utf-8")
    sh("kyb --root . run --suite smoke --agent qa_p --tag A --seed 42")
    # current B：把 EXAM-001 预设答案改成错误 -> 成功率下降
    txt = Path("config/agents/qa_p.yaml").read_text()
    txt = txt.replace('{"count": 10, "subject": "数据结构"}', '{"count": 999, "subject": "错"}')
    Path("config/agents/qa_p.yaml").write_text(txt, encoding="utf-8")
    sh("kyb --root . run --suite smoke --agent qa_p --tag B --seed 42")


def t_exit_codes():
    line("[B-退出码] 实测 `echo $?`")
    setup()
    cases = [
        ("Δ=0 或 warn 场景（A vs B 实际为 -16.7pp fail）", "regression --suite smoke --agent qa_p --baseline A --current B", None),
        ("strict-warn 场景", "regression --suite smoke --agent qa_p --baseline A --current B --strict-warn", None),
        ("运行错误（baseline 不存在）", "regression --suite smoke --agent qa_p --baseline NOPE --current B", None),
    ]
    for desc, arg, _ in cases:
        rc, out, err = sh(f"kyb --root . {arg}")
        verdict = ""
        for ln in (out + err).splitlines():
            if "未通过" in ln or "告警" in ln or "已通过" in ln or "error" in ln.lower():
                verdict = ln.strip()
        print(f"  {desc}: exit={rc}  [{verdict[:50]}]")

    # 独立验证 exit_code_for 纯函数
    from kaoyanbench.core.regression import exit_code_for
    print(f"  exit_code_for('pass')={exit_code_for('pass')}（应 0）")
    print(f"  exit_code_for('warn')={exit_code_for('warn')}（应 0）")
    print(f"  exit_code_for('warn', strict_warn=True)={exit_code_for('warn', strict_warn=True)}（应 1）")
    print(f"  exit_code_for('fail')={exit_code_for('fail')}（应 1）")


def t_validate_exit():
    line("[B-退出码] validate：error -> 2")
    rc, out, err = sh("kyb --root . validate --suite core50")
    print(f"  正常 core50 validate exit={rc}（应 0）")
    # 制造一个错误
    import tempfile
    f = Path("benchmark/tasks/public/exam/EXAM-001/references/questions.csv")
    orig = f.read_bytes(); b = bytearray(orig); b[0] ^= 1; f.write_bytes(bytes(b))
    rc2, _, _ = sh("kyb --root . validate --suite core50")
    f.write_bytes(orig)
    print(f"  篡改 fixture 后 validate exit={rc2}（应 2）")


def t_grade_bug():
    line("[缺陷复现] grade --run-id 崩溃（GradeResult.error_codes 不存在）")
    shutil.rmtree("results", ignore_errors=True)
    sh("kyb --root . run --agent mock --task HAL-001 --split public --seed 42")
    run_id = sorted(p.stem for p in Path("results/runs").glob("*.jsonl"))[0]
    rc, out, err = sh(f"kyb --root . grade --run-id {run_id}")
    print(f"  人类可读模式 exit={rc}")
    print("  stdout/stderr：")
    for ln in (out + err).splitlines():
        print("   ", ln)
    rc2, out2, err2 = sh(f"kyb --root . --json grade --run-id {run_id}")
    print(f"  --json 模式 exit={rc2}  stdout={out2.strip()!r}")
    print(f"  参考：GradeResult 字段无 error_codes（cli.py:565,574 引用）")


if __name__ == "__main__":
    t_exit_codes()
    t_validate_exit()
    t_grade_bug()
    line("退出码与缺陷实测完成")
