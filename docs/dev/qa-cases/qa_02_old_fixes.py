#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qa_02_old_fixes.py —— 独立复现 4 个「已修复」旧缺陷 + 其边界。

在 /tmp/kb_lab（qa_01 克隆出的隔离环境）内运行：
    cd /tmp/kb_lab && python3 /workspace/KaoyanBench/docs/dev/qa-cases/qa_02_old_fixes.py

不读开发者的自测结论，全部实测。
"""
from __future__ import annotations

import sys


def line(t):
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


def test_fix1_suite():
    line("[FIX-1] --suite 曾抛 TypeError: 'Suite' object is not iterable")
    from kaoyanbench.core.registry import load_suite, load_suite_tasks
    from kaoyanbench.core.config import load_suite_manifest, find_project_root

    root = find_project_root(".")
    s = load_suite("benchmark/suites/smoke.yaml", root / "benchmark" / "tasks")
    tasks = load_suite_tasks(s, root / "benchmark" / "tasks")
    assert isinstance(tasks, list), f"期望 list，实际 {type(tasks)}"
    assert len(tasks) == 6, f"smoke 期望 6 题，实际 {len(tasks)}"
    m = load_suite_manifest("benchmark/suites/smoke.yaml")
    t2 = load_suite_tasks(m, root / "benchmark" / "tasks")
    assert isinstance(t2, list) and len(t2) == 6
    print(f"  PASS: load_suite_tasks -> list[{len(tasks)}]（已解析路径与未解析 manifest 两条路径均可）")

    # 边界：suite 引用不存在的 task_id
    from kaoyanbench.core.registry import SuiteValidationError
    import tempfile, os
    bad = root / "benchmark" / "suites" / "_qa_missing.yaml"
    bad.write_text("id: _qa_missing\nsplit: public\ntask_ids: [NO-SUCH-999]\n", encoding="utf-8")
    try:
        load_suite(bad, root / "benchmark" / "tasks")
        print("  FAIL: 引用不存在 task_id 未报错")
    except SuiteValidationError as e:
        print(f"  PASS: 引用不存在 task_id -> SuiteValidationError：{str(e)[:60]}")
    finally:
        os.remove(bad)


def test_fix2_agentspec():
    line("[FIX-2] AgentSpec.from_dict params 双层嵌套")
    from kaoyanbench.core.models import AgentSpec

    s = AgentSpec.from_dict({"name": "m", "params": {"answers": {"A": "1"}, "foo": 2}})
    assert s.params == {"foo": 2}, f"params 仍被双层包裹：{s.params}"
    assert s.answers == {"A": "1"}, f"answers 未解出：{s.answers}"
    print(f"  PASS: params={s.params} answers={s.answers}（无 'params' 嵌套键）")

    # 顶层 answers
    s2 = AgentSpec.from_dict({"name": "m", "answers": {"X": "y"}})
    assert s2.answers == {"X": "y"} and s2.params == {}
    print(f"  PASS: 顶层 answers={s2.answers}")

    # 边界：两处同时给且冲突
    s3 = AgentSpec.from_dict({
        "name": "m",
        "answers": {"k": "TOP", "only_top": "T"},
        "params": {"answers": {"k": "NESTED", "only_nested": "N"}},
    })
    print(f"  边界(冲突)：answers={s3.answers}  params={s3.params}")
    print(f"  -> 同名键 k 解析为 {s3.answers.get('k')!r}（顶层优先，已文档化）")

    # 边界：answers 非 mapping
    s4 = AgentSpec.from_dict({"name": "m", "answers": ["bad"]})
    print(f"  边界(类型错)：answers 传 list -> {s4.answers}（不崩，静默忽略）")

    # 往返
    s5 = AgentSpec.from_dict(s3.to_dict())
    assert s5.answers == s3.answers, "to_dict/from_dict 往返 answers 不一致"
    print("  PASS: to_dict/from_dict 往返一致")


def test_fix3_json_schema():
    line("[FIX-3] json_schema 对 array 的 minLength 语义")
    from kaoyanbench.core.checks import validate_json_schema as V

    cases = [
        ([1, 2], {"type": "array", "minLength": 3}, False, "array len2 < minLength3"),
        ([1, 2, 3], {"type": "array", "minLength": 3}, True, "array len3 == minLength3（恰好边界）"),
        ([], {"type": "array", "minItems": 1}, False, "array len0 < minItems1"),
        ([1], {"type": "array", "minItems": 1}, True, "array len1 == minItems1（恰好边界）"),
        ([1, 2, 3], {"type": "array", "maxItems": 2}, False, "array len3 > maxItems2"),
        ([1, 2], {"type": "array", "maxItems": 2}, True, "array len2 == maxItems2（恰好边界）"),
        ("ab", {"type": "string", "minLength": 3}, False, "str len2 < minLength3"),
        ("abc", {"type": "string", "minLength": 3}, True, "str len3 == minLength3"),
        ("中文", {"type": "string", "minLength": 3}, False, "str CJK2 < minLength3"),
        ([1, 2, 3], {"type": "array", "minLength": 2, "minItems": 5}, False, "混合 min 取 max"),
    ]
    ok = 0
    for node, schema, expect_pass, desc in cases:
        problems = V(node, schema)
        got = not problems
        status = "PASS" if got == expect_pass else "FAIL"
        if got == expect_pass:
            ok += 1
        print(f"  [{status}] {desc}: {'通过' if got else '拦截'} {problems or ''}")
    assert ok == len(cases), f"{len(cases)-ok} 个边界用例不符"
    print(f"  PASS: {ok}/{len(cases)} 边界全部符合 JSON-Schema 语义")


def test_fix4_report_category():
    line("[FIX-4] cmd_report 的 tasks[].category/difficulty/network 曾为 unknown")
    import subprocess, json
    subprocess.run(["kyb", "--root", ".", "run", "--suite", "smoke", "--agent", "mock",
                    "--tag", "_qa_fix4", "--seed", "42"], capture_output=True)
    subprocess.run(["kyb", "--root", ".", "report", "--suite", "smoke", "--agent", "mock",
                    "--tag", "_qa_fix4", "--format", "json"], capture_output=True)
    d = json.load(open("reports/smoke__mock___qa_fix4/report.json"))
    unknown = [t["task_id"] for t in d["tasks"]
               if t["category"] == "unknown" or t["difficulty"] == "unknown" or t["network"] == "unknown"]
    assert not unknown, f"仍为 unknown：{unknown}"
    print(f"  PASS: {len(d['tasks'])} 条明细全部回填 category/difficulty/network，无 unknown")
    print(f"  errors 类别数：{len(d['errors'])}（应 11）")
    assert len(d["errors"]) == 11


if __name__ == "__main__":
    test_fix1_suite()
    test_fix2_agentspec()
    test_fix3_json_schema()
    test_fix4_report_category()
    line("全部旧缺陷复现完成：以上 PASS 均为实测通过")
