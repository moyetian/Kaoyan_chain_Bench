#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_private_tasks.py —— 首批 private 隐藏集生成器（P2，10 题）。

设计铁律
--------
1. **确定性**：全部 offline + deterministic，无墙钟、无随机；同一份源码
   连续运行两次，``task.json`` / ``references/*`` / ``manifest.json``
   逐字节一致（``--check`` 可校验）。
2. **零第三方依赖**：只用标准库。
3. **全虚构、防污染**：使用与 public 不同的虚构院校（西泽大学 / 北辰理工 /
   南枫大学），每个文本文件头写明虚构；题型覆盖 planning / hallucination /
   exam / university 四类，难度 easy 3 / medium 4 / hard 2 / expert 1，
   与 public 集同构但数据零重叠（过拟合探测：public 高分 + private 低分
   即疑似背题）。
4. **自足可跑**：带 ground_truth 的 offline 题必配 ``references/`` +
   ``manifest.json``（sha256），满足 ``validate_tasks`` 硬规则；
   ground_truth 为 null 的约束/抗幻觉题不需要 fixture。

用法
----
    python tools/build_private_tasks.py            # 生成全部（幂等）
    python tools/build_private_tasks.py --check    # 只校验，不写入；不一致则退出码 1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_ROOT = ROOT / "benchmark" / "tasks" / "private"

VERSION = "1.1"

DISCLAIMER = "虚构数据，仅用于基准测试（KaoyanBench v1.1 private）；不对应任何真实院校、专业或人员。"

SCHOOLS = {
    "xize": "西泽大学",
    "beichen": "北辰理工大学",
    "nanfeng": "南枫大学",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _task_path(category: str, task_id: str) -> Path:
    return PRIVATE_ROOT / category / task_id


def _write_text(path: Path, text: str) -> None:
    # 注意：必须按字节写入（禁止 write_text 的换行符翻译），否则 Windows 下
    # \n 会被写成 \r\n，导致 manifest.json 的 sha256 与 validate 校验对不上
    #（与 tools/gen_fixtures.py 的 _write_bytes 行为一致，保证跨平台逐字节稳定）。
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _write_task(task: dict) -> None:
    d = _task_path(task["category"], task["task_id"])
    _write_text(d / "task.json", json.dumps(task, ensure_ascii=False, indent=2) + "\n")


def _write_readme(category: str, task_id: str, body: str) -> None:
    _write_text(_task_path(category, task_id) / "README.md",
                f"# {task_id}（private）\n\n{DISCLAIMER}\n\n{body}\n")


def _write_references(category: str, task_id: str, files: dict[str, str]) -> None:
    refs = _task_path(category, task_id) / "references"
    manifest_files = {}
    for name, text in sorted(files.items()):
        data = text.encode("utf-8")
        _write_text(refs / name, text)
        manifest_files[name] = {"sha256": _sha(data), "size": len(data)}
    manifest = {"files": manifest_files, "task_id": task_id, "version": VERSION}
    _write_text(refs / "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def _base(task_id: str, category: str, difficulty: str, instruction: str,
          tags: list[str]) -> dict:
    return {
        "task_id": task_id,
        "category": category,
        "difficulty": difficulty,
        "instruction": instruction,
        "network": "offline",
        "time_limit": 900,
        "tool_limit": 60,
        "split": "private",
        "tags": tags,
        "version": VERSION,
    }


def _planning(task_id: str, difficulty: str, days: int, hours: float,
              subjects: list[tuple[str, float]]) -> dict:
    subj_text = "、".join(f"{name}不少于{sub}小时" for name, sub in subjects)
    task = _base(
        task_id, "planning", difficulty,
        f"帮我排一份 {days} 天的复习日程。每天总共排 {hours:g} 小时，其中{subj_text}；"
        f"时段之间不要打架。用 schedule 字段给出 JSON 数组，每天一项（含 day、"
        f"total_hours、slots）。",
        ["deterministic", "constraint", "private"],
    )
    rules: list[dict] = [
        {"kind": "total_days", "op": "eq", "value": days},
        {"kind": "daily_total_hours", "op": "eq", "value": hours},
    ]
    for name, sub in subjects:
        rules.append({"kind": "subject_min_hours", "subject": name,
                      "op": "ge", "value": sub})
    rules.append({"kind": "no_overlap", "value": True})
    task["expected"] = {
        "must_find": [{"id": "p1", "weight": 1.0, "dimension": "factuality",
                       "critical": True, "any_of": [subjects[0][0], subjects[1][0]]}],
        "required_sources": [],
        "must_not_claim": [],
        "ground_truth": None,
    }
    task["grader"] = {
        "type": "deterministic",
        "pass_threshold": 60.0,
        "checks": [
            {"id": "c1", "type": "constraint", "dimension": "task_completion",
             "critical": True, "answer_path": "$.schedule", "rules": rules},
            {"id": "c2", "type": "json_schema", "dimension": "completeness",
             "path": "$.schedule",
             "schema": {"type": "array", "minLength": days}},
        ],
        "rubric": None,
    }
    task["answer_format"] = {"kind": "json", "path": "answer"}
    return task


def _hallucination(task_id: str, difficulty: str, instruction: str,
                   find_any: list[str], forbid: list[str],
                   extra_checks: list[dict] | None = None) -> dict:
    task = _base(task_id, "hallucination", difficulty, instruction,
                 ["deterministic", "anti_hallucination", "private"])
    task["expected"] = {
        "must_find": [{"id": "p1", "weight": 1.0, "dimension": "factuality",
                       "critical": True, "any_of": find_any}],
        "required_sources": [],
        "must_not_claim": forbid,
        "ground_truth": None,
    }
    checks: list[dict] = [
        # 注意：string_contains 只认单值 value（见 checks.check_string_contains），
        # 不认 any_of；此处与 public HAL-001 写法对齐，取首个要点词 + path $.answer。
        {"id": "c1", "type": "string_contains", "dimension": "factuality",
         "path": "$.answer", "value": find_any[0], "critical": True},
        {"id": "c2", "type": "must_not_claim", "dimension": "factuality",
         "critical": True, "patterns": forbid},
    ]
    checks.extend(extra_checks or [])
    task["grader"] = {"type": "deterministic", "pass_threshold": 60.0,
                      "checks": checks, "rubric": None}
    task["answer_format"] = {"kind": "text", "path": "answer"}
    return task


def _exam_count() -> tuple[dict, dict[str, str], str]:
    rows = [
        ("1", "数据结构", "线性表"), ("2", "操作系统", "进程管理"),
        ("3", "计算机网络", "传输层"), ("4", "数据结构", "树"),
        ("5", "组成原理", "指令系统"), ("6", "操作系统", "内存管理"),
        ("7", "数据结构", "图"), ("8", "计算机网络", "网络层"),
        ("9", "操作系统", "文件系统"), ("10", "组成原理", "存储器"),
        ("11", "计算机网络", "应用层"), ("12", "操作系统", "设备管理"),
    ]
    csv_text = "id,subject,chapter\n" + "".join(f"{i},{s},{c}\n" for i, s, c in rows)
    task = _base(
        "EXAM-101", "exam", "easy",
        "统计真题 CSV 中考点『操作系统』出现的次数，输出精确整数。仅依据本地 fixture。",
        ["fixture", "deterministic", "count", "private"],
    )
    task["expected"] = {
        "must_find": [{"id": "p1", "weight": 1.0, "dimension": "factuality",
                       "critical": True, "any_of": ["操作系统"]}],
        "required_sources": [],
        "must_not_claim": [],
        "ground_truth": {"count": 4, "subject": "操作系统"},
    }
    task["grader"] = {
        "type": "deterministic", "pass_threshold": 60.0,
        "checks": [
            {"id": "c1", "type": "numeric", "dimension": "factuality",
             "path": "$.count", "op": "eq", "value": 4, "tol": 0.0},
            {"id": "c2", "type": "point_hit", "dimension": "factuality",
             "point_id": "p1"},
        ],
        "rubric": None,
    }
    task["answer_format"] = {"kind": "json", "path": "answer"}
    readme = ("考点：CSV fixture 计数（操作系统出现 4 次，共 12 行）。"
              "陷阱：不得凭记忆作答，必须以 references/questions.csv 为准；"
              "ground_truth 依据 questions.csv 人工计数。")
    return task, {"questions.csv": csv_text}, readme


def _exam_avg() -> tuple[dict, dict[str, str], str]:
    csv_text = "year,min_score\n2022,318\n2023,326\n2024,334\n"
    task = _base(
        "EXAM-102", "exam", "medium",
        "根据本地 fixture 中 2022—2024 年录取最低分，计算三年平均分（保留一位小数），"
        "输出 JSON（含 avg 字段）。仅依据本地 fixture。",
        ["fixture", "deterministic", "average", "private"],
    )
    task["expected"] = {
        "must_find": [{"id": "p1", "weight": 1.0, "dimension": "factuality",
                       "critical": True, "any_of": ["平均分"]}],
        "required_sources": [],
        "must_not_claim": [],
        "ground_truth": {"avg": 326.0, "years": [2022, 2023, 2024]},
    }
    task["grader"] = {
        "type": "deterministic", "pass_threshold": 60.0,
        "checks": [
            {"id": "c1", "type": "numeric", "dimension": "factuality",
             "path": "$.avg", "op": "eq", "value": 326.0, "tol": 0.5},
            {"id": "c2", "type": "point_hit", "dimension": "factuality",
             "point_id": "p1"},
        ],
        "rubric": None,
    }
    task["answer_format"] = {"kind": "json", "path": "answer"}
    readme = ("考点：fixture 数值平均（(318+326+334)/3=326.0）。"
              "陷阱：年份错位、整数除法；ground_truth 依据 scores.csv 计算。")
    return task, {"scores.csv": csv_text}, readme


def _uni_xize() -> tuple[dict, dict[str, str], str]:
    excerpt = (
        f"{DISCLAIMER}\n"
        f"{SCHOOLS['xize']}研究生院2026年硕士专业目录（摘录）：\n"
        "083500 软件工程，拟招生30人，初试科目：思想政治理论、英语一、数学一、软件工程基础。\n"
        "085400 计算机技术，拟招生45人，初试科目：思想政治理论、英语一、数学一、数据结构。\n"
        "081200 计算机科学与技术，拟招生25人，初试科目：思想政治理论、英语一、数学一、操作系统。\n"
    )
    task = _base(
        "UNI-101", "university", "medium",
        f"从虚构院校『{SCHOOLS['xize']}』的本地专业目录摘录（references/excerpt.txt）中提取 "
        "3 个专业的：专业代码、拟招生人数、初试科目中的专业业务课。仅依据本地 fixture，不得联网。",
        ["fixture", "deterministic", "mirror", "private"],
    )
    task["expected"] = {
        "must_find": [{"id": "p1", "weight": 1.0, "dimension": "factuality",
                       "critical": True,
                       "any_of": ["083500", "085400", "081200"]}],
        "required_sources": [],
        "must_not_claim": ["该校实际招生人数为"],
        "ground_truth": {"majors": [
            {"code": "083500", "plan": 30, "subject": "软件工程基础"},
            {"code": "085400", "plan": 45, "subject": "数据结构"},
            {"code": "081200", "plan": 25, "subject": "操作系统"},
        ]},
    }
    task["grader"] = {
        "type": "deterministic", "pass_threshold": 60.0,
        "checks": [
            {"id": "c1", "type": "point_hit", "dimension": "factuality",
             "any_of": ["083500", "085400", "081200"], "critical": True},
            {"id": "c2", "type": "set_includes", "dimension": "completeness",
             "values": ["083500", "085400", "081200"], "min_hits": 3},
            {"id": "c3", "type": "json_schema", "dimension": "completeness",
             "path": "$.majors",
             "schema": {"type": "array", "minLength": 3,
                        "items": {"type": "object",
                                  "required": ["code", "plan", "subject"]}}},
            {"id": "c4", "type": "numeric", "dimension": "factuality",
             "path": "$.majors[0].plan", "op": "eq", "value": 30, "tol": 0.5},
        ],
        "rubric": None,
    }
    task["answer_format"] = {"kind": "json", "path": "answer"}
    readme = ("考点：fixture 多专业信息提取（代码/人数/业务课一一对应）。"
              "陷阱：把公共课当业务课、人数错位；ground_truth 依据 excerpt.txt。")
    return task, {"excerpt.txt": excerpt}, readme


def _uni_beichen() -> tuple[dict, dict[str, str], str]:
    excerpt = (
        f"{DISCLAIMER}\n"
        f"{SCHOOLS['beichen']}2026年硕士招生专业对照（摘录）：\n"
        "学硕：081200 计算机科学与技术，拟招生18人，初试业务课：数学一、操作系统。\n"
        "专硕：085400 计算机技术，拟招生42人，初试业务课：数学一、数据结构。\n"
        "专硕：083900 网络空间安全，拟招生22人，初试业务课：数学一、计算机网络。\n"
    )
    task = _base(
        "UNI-102", "university", "hard",
        f"同一所虚构院校『{SCHOOLS['beichen']}』同时招收 A、B 两类硕士研究生"
        "（类别名称以对照表原文为准），请从本地对照表"
        "（references/excerpt.txt）中整理每个专业的：类别名称、专业代码与对应"
        "初试业务课。仅依据本地 fixture，不得联网，更不得凭空编造代码。",
        ["fixture", "deterministic", "mirror", "private"],
    )
    task["expected"] = {
        "must_find": [{"id": "p1", "weight": 1.0, "dimension": "factuality",
                       "critical": True, "any_of": ["学硕", "学术"]},
                      {"id": "p2", "weight": 1.0, "dimension": "factuality",
                       "critical": False, "any_of": ["专硕", "专业学位"]}],
        "required_sources": [],
        "must_not_claim": ["学硕专硕代码完全相同"],
        "ground_truth": {"majors": [
            {"code": "081200", "plan": 18, "subject": "操作系统", "kind": "学硕"},
            {"code": "085400", "plan": 42, "subject": "数据结构", "kind": "专硕"},
            {"code": "083900", "plan": 22, "subject": "计算机网络", "kind": "专硕"},
        ]},
    }
    task["grader"] = {
        "type": "deterministic", "pass_threshold": 60.0,
        "checks": [
            {"id": "c1", "type": "point_hit", "dimension": "factuality",
             "any_of": ["学硕", "学术"], "critical": True},
            {"id": "c2", "type": "point_hit", "dimension": "factuality",
             "any_of": ["专硕", "专业学位"]},
            {"id": "c3", "type": "set_includes", "dimension": "completeness",
             "values": ["081200", "085400", "083900"], "min_hits": 3},
            {"id": "c4", "type": "numeric", "dimension": "factuality",
             "path": "$.majors[1].plan", "op": "eq", "value": 42, "tol": 0.5},
            {"id": "c5", "type": "must_not_claim", "dimension": "factuality",
             "critical": True, "patterns": ["学硕专硕代码完全相同"]},
        ],
        "rubric": None,
    }
    task["answer_format"] = {"kind": "json", "path": "answer"}
    readme = ("考点：学硕/专硕对照提取（类型-代码-业务课三元对齐）。"
              "陷阱：类型与代码张冠李戴、臆测代码；ground_truth 依据 excerpt.txt。")
    return task, {"excerpt.txt": excerpt}, readme


def _all_tasks() -> list[tuple[dict, dict[str, str] | None, str]]:
    out: list[tuple[dict, dict[str, str] | None, str]] = []
    out.append((_planning("PLAN-101", "easy", 21, 5.0,
                          [("英语", 1.5), ("数学", 2.0), ("专业课", 1.5)]),
                None,
                "考点：21 天冲刺日程约束满足。陷阱：天数/学时张冠李戴、时段重叠；纯约束题 ground_truth=null。"))
    out.append((_planning("PLAN-102", "medium", 45, 3.0,
                          [("数学", 1.0), ("英语", 1.0), ("专业课", 1.0)]),
                None,
                "考点：45 天在职备考均匀排程。陷阱：工作日/周末混排导致日学时不等；纯约束题 ground_truth=null。"))
    out.append((_planning("PLAN-103", "hard", 120, 7.0,
                          [("数学", 2.5), ("英语", 1.5), ("专业课", 2.5), ("政治", 0.5)]),
                None,
                "考点：120 天长周期四科排程。陷阱：小数课时求和漂移、遗漏政治；纯约束题 ground_truth=null。"))
    out.append((_hallucination(
        "HAL-101", "easy",
        f"请查询虚构院校『{SCHOOLS['xize']}』2027 年硕士招生计划人数。"
        "如果查不到官方数据，请如实说明，不要编造数字。",
        ["未找到", "没有官方", "无法确认"],
        ["招生人数为", "扩招至"],
        [{"id": "c3", "type": "regex", "dimension": "factuality",
          "pattern": "(没有|无|未).{0,6}(官方|数据|信息)"}]),
        None,
        "考点：查无数据时如实承认（西泽大学 2027 招生计划为虚构陷阱）。"
        "陷阱：编造具体人数；ground_truth=null，只判要点+禁断言。"))
    out.append((_hallucination(
        "HAL-102", "medium",
        f"网传『{SCHOOLS['beichen']}新增人工智能博士点已获批』，请核实。"
        "如无官方依据，不得断言已获批，请说明核实结论。",
        ["未证实", "无法确认", "以官方为准"],
        ["已获批", "正式设立", "已经设立"]),
        None,
        "考点：网传消息无官方依据时不得转述为事实。"
        "陷阱：把传闻当结论；ground_truth=null，只判要点+禁断言。"))
    out.append((_hallucination(
        "HAL-103", "expert",
        f"网传『{SCHOOLS['nanfeng']}2026 年复试线为 352 分』，请判断该说法是否可采信。"
        "如无官方出处，不得采信具体分数，须说明以官方公布为准。",
        ["以官方为准", "以官网为准", "尚未公布"],
        ["复试线确定为352", "已经确定为352"]),
        None,
        "考点：网传分数线不可采信（352 为虚构陷阱数字）。"
        "陷阱：复述陷阱数字即违规；ground_truth=null，只判要点+禁断言。"))
    t, refs, readme = _exam_count()
    out.append((t, refs, readme))
    t, refs, readme = _exam_avg()
    out.append((t, refs, readme))
    t, refs, readme = _uni_xize()
    out.append((t, refs, readme))
    t, refs, readme = _uni_beichen()
    out.append((t, refs, readme))
    return out


def build(*, check: bool = False) -> int:
    failures: list[str] = []
    for task, refs, readme in _all_tasks():
        task_id, category = task["task_id"], task["category"]
        payload = json.dumps(task, ensure_ascii=False, indent=2) + "\n"
        readme_text = (f"# {task_id}（private）\n\n{DISCLAIMER}\n\n{readme}\n")
        if check:
            base = _task_path(category, task_id)
            try:
                current = (base / "task.json").read_text(encoding="utf-8")
            except OSError:
                failures.append(f"{task_id}：缺 task.json")
                continue
            if current != payload:
                failures.append(f"{task_id}：task.json 与生成器不一致（请重跑本脚本）")
            try:
                current_readme = (base / "README.md").read_text(encoding="utf-8")
            except OSError:
                failures.append(f"{task_id}：缺 README.md")
                continue
            if current_readme != readme_text:
                failures.append(f"{task_id}：README.md 与生成器不一致")
            if refs is not None:
                refs_dir = base / "references"
                for name, text in sorted(refs.items()):
                    try:
                        current_ref = (refs_dir / name).read_bytes()
                    except OSError:
                        failures.append(f"{task_id}：缺 references/{name}")
                        break
                    if current_ref != text.encode("utf-8"):
                        failures.append(f"{task_id}：references/{name} 不一致")
                        break
                else:
                    data = {(n): {"sha256": _sha(t.encode("utf-8")),
                                  "size": len(t.encode("utf-8"))}
                            for n, t in sorted(refs.items())}
                    manifest = {"files": data, "task_id": task_id, "version": VERSION}
                    expected_manifest = (json.dumps(manifest, ensure_ascii=False,
                                                   indent=2) + "\n").encode("utf-8")
                    try:
                        current_manifest = (refs_dir / "manifest.json").read_bytes()
                    except OSError:
                        failures.append(f"{task_id}：缺 references/manifest.json")
                        continue
                    if current_manifest != expected_manifest:
                        failures.append(f"{task_id}：manifest.json 不一致")
            continue
        _write_task(task)
        _write_readme(category, task_id, readme)
        if refs is not None:
            _write_references(category, task_id, refs)
    if check:
        if failures:
            print("私测集校验未通过：")
            for line in failures:
                print(f"  - {line}")
            return 1
        print("私测集校验通过：10 题（planning 3 / hallucination 3 / exam 2 / university 2）")
        return 0
    print("私测集生成完毕：10 题（planning 3 / hallucination 3 / exam 2 / university 2）"
          "（幂等，可重复运行）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成首批 private 隐藏集（10 题）")
    parser.add_argument("--check", action="store_true", help="只校验，不写入")
    args = parser.parse_args(argv)
    return build(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
