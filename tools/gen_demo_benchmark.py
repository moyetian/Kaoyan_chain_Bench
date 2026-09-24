#!/usr/bin/env python3
"""为**报告渲染验收**生成一个临时的 KaoyanBench 演示项目副本（后端无侵入）。

用途：在不依赖 ``core50`` 真实任务、不消耗任何模型额度的情况下，
造出一个「指标非空」的完整项目副本，用于端到端验证报告渲染层
（F-02/F-03/F-04）与回归对比章节。

本脚本**不改动仓库内任何既有文件**（不写 ``benchmark/tasks/``、不改 ``config/agents/mock.yaml``），
而是：

1. 把项目树复制到 ``--out``（默认 ``<系统临时目录>/kaoyanbench-demo``，跨平台）；
2. 在副本里生成 14 个覆盖 8 类的**全离线确定性任务** + 1 张 demo suite manifest；
3. 在副本里写一份 ``config/agents/mock.yaml`` 预设答案集（含 sources / citations / usage），
   使 mock Runner 能真正产出**非空**的 metrics（latency / cost / citation 等）；
4. 打印后续命令。

结果：
- 忠实（真实数值，非伪造报告）；
- 可复现（固定内容，脚本幂等，重复运行结果一致）；
- 零侵入（原仓库文件哈希不变）。

用法::

    python tools/gen_demo_benchmark.py                    # 默认输出到系统临时目录
    python tools/gen_demo_benchmark.py --out ./demo-copy  # 或显式指定
    cd <out>
    kaoyanbench run --suite demo --agent mock --tag v1 --seed 42
    kaoyanbench report --suite demo --agent mock --tag v1
    kaoyanbench run --suite demo --agent mock --tag v2 --seed 42
    kaoyanbench report --suite demo --agent mock --tag v2 --baseline v1
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 默认输出目录（跨平台：Windows 用 %TEMP%，POSIX 用 /tmp）
DEFAULT_OUT = str(Path(tempfile.gettempdir()) / "kaoyanbench-demo")

# --------------------------------------------------------------------------- #
# 任务定义（8 类 / 4 难度；全 offline + deterministic grader，无网络依赖）
# --------------------------------------------------------------------------- #
# 每个元素：task_id, category, difficulty, instruction, checks, expected
TASKS: list[dict[str, Any]] = [
    # ---------------- search ----------------
    {
        "task_id": "SEARCH-001",
        "category": "search",
        "difficulty": "easy",
        "instruction": "给出虚构示例院校 2025 年计算机专业复试线，并附官方来源。",
        "points": [{"id": "p1", "any_of": ["复试线"], "all_of": []}],
        "checks": [
            {"id": "c1", "type": "point_hit", "dimension": "factuality", "any_of": ["复试线"]},
            {"id": "c2", "type": "regex", "dimension": "factuality", "pattern": r"3[0-9]{2}"},
            {"id": "c3", "type": "year_tag", "dimension": "factuality", "year": 2025},
            {"id": "c4", "type": "source_level", "dimension": "source_quality",
             "level_min": "E4", "min_count": 1},
            {"id": "c5", "type": "citation_coverage", "dimension": "citation",
             "min_citations": 1, "min_supported_ratio": 0.8},
        ],
    },
    {
        "task_id": "SEARCH-002",
        "category": "search",
        "difficulty": "medium",
        "instruction": "对比两所虚构院校的招生人数差异，并给出计算过程。",
        "points": [{"id": "p1", "any_of": ["招生人数"], "all_of": []}],
        "checks": [
            {"id": "c1", "type": "point_hit", "dimension": "factuality", "any_of": ["招生人数"]},
            {"id": "c2", "type": "numeric", "dimension": "completeness", "path": "$.difference",
             "value": 12, "op": "eq", "tol": 0.5},
            {"id": "c3", "type": "source_domain", "dimension": "source_quality",
             "domain_suffix": [".edu.cn"], "min_count": 1},
        ],
    },
    {
        "task_id": "SEARCH-003",
        "category": "search",
        "difficulty": "hard",
        "instruction": "提炼复试流程要点，并说明信息来源年份。",
        "points": [{"id": "p1", "any_of": ["复试流程"], "all_of": []}],
        "checks": [
            {"id": "c1", "type": "point_hit", "dimension": "factuality", "any_of": ["复试流程"]},
            {"id": "c2", "type": "year_tag", "dimension": "factuality", "year": 2025},
            {"id": "c3", "type": "source_level", "dimension": "source_quality",
             "level_min": "E4", "min_count": 1},
            {"id": "c4", "type": "citation_coverage", "dimension": "citation",
             "min_citations": 2, "min_supported_ratio": 0.9},
        ],
    },
    # ---------------- university ----------------
    {
        "task_id": "UNI-001",
        "category": "university",
        "difficulty": "easy",
        "instruction": "汇总虚构示例大学研究生院官网的招生简章要点。",
        "points": [{"id": "p1", "any_of": ["招生简章"], "all_of": []}],
        "checks": [
            {"id": "c1", "type": "point_hit", "dimension": "factuality", "any_of": ["招生简章"]},
            {"id": "c2", "type": "source_domain", "dimension": "source_quality",
             "domain_suffix": [".edu.cn"], "min_count": 1},
            {"id": "c3", "type": "source_level", "dimension": "source_quality",
             "level_min": "E4", "min_count": 1},
        ],
    },
    {
        "task_id": "UNI-002",
        "category": "university",
        "difficulty": "medium",
        "instruction": "从招生目录中提取专业代码与考试科目。",
        "points": [{"id": "p1", "any_of": ["专业代码"], "all_of": []}],
        "checks": [
            {"id": "c1", "type": "point_hit", "dimension": "factuality", "any_of": ["专业代码"]},
            {"id": "c2", "type": "string_contains", "dimension": "completeness",
             "value": "考试科目"},
            {"id": "c3", "type": "source_domain", "dimension": "source_quality",
             "domain_suffix": [".edu.cn"], "min_count": 1},
        ],
    },
    # ---------------- policy ----------------
    {
        "task_id": "POLICY-001",
        "category": "policy",
        "difficulty": "medium",
        "instruction": "说明 2025 年推免政策对应届生的影响，并给出政策原文来源。",
        "points": [{"id": "p1", "any_of": ["推免"], "all_of": []}],
        "checks": [
            {"id": "c1", "type": "point_hit", "dimension": "factuality", "any_of": ["推免"]},
            {"id": "c2", "type": "year_tag", "dimension": "factuality", "year": 2025},
            {"id": "c3", "type": "source_level", "dimension": "source_quality",
             "level_min": "E5", "min_count": 1},
            {"id": "c4", "type": "citation_coverage", "dimension": "citation",
             "min_citations": 1, "min_supported_ratio": 1.0},
        ],
    },
    {
        "task_id": "POLICY-002",
        "category": "policy",
        "difficulty": "hard",
        "instruction": "解读调剂规则的变更点，注意不要虚构未发布的条款。",
        "points": [{"id": "p1", "any_of": ["调剂"], "all_of": []}],
        "must_not_claim": ["全国统一调剂线为 300 分"],
        "checks": [
            {"id": "c1", "type": "point_hit", "dimension": "factuality", "any_of": ["调剂"]},
            {"id": "c2", "type": "must_not_claim", "dimension": "factuality",
             "patterns": ["全国统一调剂线为 300 分"]},
            {"id": "c3", "type": "source_level", "dimension": "source_quality",
             "level_min": "E4", "min_count": 1},
        ],
    },
    # ---------------- exam ----------------
    {
        "task_id": "EXAM-001",
        "category": "exam",
        "difficulty": "medium",
        "instruction": "分析历年分数线趋势，给出平均分与最高分。",
        "points": [{"id": "p1", "any_of": ["分数线"], "all_of": []}],
        "checks": [
            {"id": "c1", "type": "point_hit", "dimension": "factuality", "any_of": ["分数线"]},
            {"id": "c2", "type": "numeric", "dimension": "completeness", "path": "$.average",
             "value": 330, "op": "eq", "tol": 1.0},
            {"id": "c3", "type": "numeric", "dimension": "completeness", "path": "$.max",
             "value": 350, "op": "eq", "tol": 0.5},
            {"id": "c4", "type": "regex", "dimension": "factuality", "pattern": r"趋势"},
        ],
    },
    {
        "task_id": "EXAM-002",
        "category": "exam",
        "difficulty": "hard",
        "instruction": "从真题统计表中计算各题型占比。",
        "points": [{"id": "p1", "any_of": ["题型"], "all_of": []}],
        "checks": [
            {"id": "c1", "type": "point_hit", "dimension": "factuality", "any_of": ["题型"]},
            {"id": "c2", "type": "set_includes", "dimension": "completeness",
             "value": ["选择题", "填空题", "解答题"]},
        ],
    },
    # ---------------- pdf ----------------
    {
        "task_id": "PDF-001",
        "category": "pdf",
        "difficulty": "medium",
        "instruction": "从 PDF 招生简章中提取报名时间与考试时间。",
        "points": [{"id": "p1", "any_of": ["报名时间"], "all_of": []}],
        "checks": [
            {"id": "c1", "type": "point_hit", "dimension": "factuality", "any_of": ["报名时间"]},
            {"id": "c2", "type": "regex", "dimension": "completeness", "pattern": r"2025-1[0-2]-\d{2}"},
        ],
    },
    {
        "task_id": "PDF-002",
        "category": "pdf",
        "difficulty": "hard",
        "instruction": "对 PDF 中的表格做结构化提取，输出 JSON。",
        "points": [{"id": "p1", "any_of": ["表格"], "all_of": []}],
        "checks": [
            {"id": "c1", "type": "point_hit", "dimension": "factuality", "any_of": ["表格"]},
            {"id": "c2", "type": "json_schema", "dimension": "completeness",
             "required_keys": ["rows", "columns"]},
        ],
    },
    # ---------------- planning ----------------
    {
        "task_id": "PLAN-001",
        "category": "planning",
        "difficulty": "medium",
        "instruction": "制定 6 个月考研复习计划，按阶段划分并给出每周任务。",
        "points": [{"id": "p1", "any_of": ["阶段"], "all_of": []}],
        "checks": [
            {"id": "c1", "type": "point_hit", "dimension": "factuality", "any_of": ["阶段"]},
            {"id": "c2", "type": "string_contains", "dimension": "completeness", "value": "每周"},
            {"id": "c3", "type": "constraint", "dimension": "task_completion",
             "must_include": ["基础", "强化", "冲刺"]},
        ],
    },
    # ---------------- research ----------------
    {
        "task_id": "RESEARCH-001",
        "category": "research",
        "difficulty": "expert",
        "instruction": "多步骤研究：检索 → 对比 → 结论，输出带引用的研究报告。",
        "points": [{"id": "p1", "any_of": ["结论"], "all_of": []}],
        "checks": [
            {"id": "c1", "type": "point_hit", "dimension": "factuality", "any_of": ["结论"]},
            {"id": "c2", "type": "point_hit", "dimension": "factuality", "any_of": ["对比"]},
            {"id": "c3", "type": "citation_coverage", "dimension": "citation",
             "min_citations": 3, "min_supported_ratio": 0.8},
            {"id": "c4", "type": "source_level", "dimension": "source_quality",
             "level_min": "E4", "min_count": 2},
        ],
    },
    # ---------------- hallucination ----------------
    {
        "task_id": "HAL-001",
        "category": "hallucination",
        "difficulty": "hard",
        "instruction": "对未公开的招生数据，必须说明「无法确认」而不是编造数字。",
        "points": [{"id": "p1", "any_of": ["无法确认"], "all_of": []}],
        "must_not_claim": ["确切人数为 999 人"],
        "checks": [
            {"id": "c1", "type": "point_hit", "dimension": "factuality", "any_of": ["无法确认"]},
            {"id": "c2", "type": "must_not_claim", "dimension": "factuality",
             "patterns": ["确切人数为 999 人"]},
            {"id": "c3", "type": "evidence_level", "dimension": "source_quality",
             "level_min": "E0", "min_count": 0},
        ],
    },
]

#: 演示用 suite manifest（与仓库内 smoke.yaml 形态一致）。
DEMO_SUITE = """\
# demo：报告渲染验收用的演示集（由 tools/gen_demo_benchmark.py 生成于项目副本中）
id: demo
split: public
version: "1.0"
description: "8 类 14 题演示集：全离线 + 确定性 grader，供报告渲染层端到端验证"
task_ids: all
filters:
  tags:
    - demo
defaults:
  runs: 3
  time_limit: 120
  tool_limit: 30
"""

#: 演示用 mock 预设答案：**忠实、可复现、含 usage/sources/citations**。
#: 注意：mock Runner 不产生真实耗时，latency 为 0；直方图与成本图另由
#: tools/check_report_html.py 的合成 fixture 覆盖（见前端说明）。
def _presets() -> dict[str, Any]:
    def src(url: str, title: str, year: int = 2025) -> dict[str, Any]:
        return {
            "url": url,
            "title": title,
            "year": year,
            "content_sha256": "0" * 8 + url[-8:],
        }

    def cit(cid: str, claim: str, ref: str, ok: bool = True) -> dict[str, Any]:
        return {
            "citation_id": cid,
            "claim": claim,
            "source_ref": ref,
            "supported": ok,
            "judge": "deterministic",
        }

    edu = "https://yz.kaoyan.example.edu.cn/zsml/2025"
    chsi = "https://yz.chsi.com.cn/zsml/2025"
    moe = "https://www.moe.gov.cn/zsjz/2025"
    blog = "https://blog.example.com/post/1"

    return {
        "SEARCH-001": {
            "final_answer": "根据 2025 年招生简章，虚构示例院校计算机专业复试线为 320 分。",
            "sources": [src(edu, "示例院校 2025 招生目录")],
            "citations": [cit("cit1", "复试线为 320 分", edu)],
            "usage": {"input_tokens": 1200, "output_tokens": 240, "cost_usd": 0.0018},
            "tool_calls": [
                {"name": "web_search", "ok": True, "arguments": {"q": "复试线 2025"}},
                {"name": "fetch_page", "ok": True, "arguments": {"url": edu}},
            ],
        },
        "SEARCH-002": {
            "final_answer": "对比可见招生人数差异为 12 人。",
            "difference": 12,
            "sources": [src(edu, "示例院校招生目录")],
            "citations": [cit("cit1", "差异 12 人", edu)],
            "usage": {"input_tokens": 1500, "output_tokens": 300, "cost_usd": 0.0024},
        },
        "SEARCH-003": {
            "final_answer": "2025 年复试流程要点包括材料提交、笔试与面试。",
            "sources": [
                src(edu, "示例院校复试办法"),
                src(chsi, "研招网复试专题"),
            ],
            "citations": [
                cit("cit1", "复试流程含笔试面试", edu),
                cit("cit2", "流程要点", chsi),
            ],
            "usage": {"input_tokens": 2100, "output_tokens": 420, "cost_usd": 0.0036},
        },
        "UNI-001": {
            "final_answer": "示例大学研究生院 2025 年招生简章要点：报名条件、招生人数与学费。",
            "sources": [src(edu, "示例大学研究生院招生简章")],
            "citations": [cit("cit1", "招生简章要点", edu)],
            "usage": {"input_tokens": 900, "output_tokens": 180, "cost_usd": 0.0013},
        },
        "UNI-002": {
            "final_answer": "专业代码 081200，考试科目为数学一与计算机专业基础。",
            "sources": [src(edu, "示例大学招生目录")],
            "usage": {"input_tokens": 1100, "output_tokens": 220, "cost_usd": 0.0016},
        },
        "POLICY-001": {
            "final_answer": "2025 年推免政策对应届生影响主要体现在名额分配与材料审核。",
            "sources": [src(moe, "教育部推免管理办法")],
            "citations": [cit("cit1", "推免名额分配", moe)],
            "usage": {"input_tokens": 1800, "output_tokens": 360, "cost_usd": 0.0031},
        },
        "POLICY-002": {
            "final_answer": "2025 年调剂规则变更点：延长开放时间、明确优先级。",  # 故意未命中 must_not_claim
            "sources": [src(moe, "教育部调剂管理规定")],
            "citations": [cit("cit1", "调剂开放时间", moe)],
            "usage": {"input_tokens": 1600, "output_tokens": 320, "cost_usd": 0.0028},
        },
        "EXAM-001": {
            "final_answer": "分数线趋势平稳，平均分 330 分，最高分 350 分。",
            "average": 330,
            "max": 350,
            "usage": {"input_tokens": 800, "output_tokens": 160, "cost_usd": 0.0012},
        },
        "EXAM-002": {
            "final_answer": "从真题统计表计算题型占比，主要包含选择题、填空题、解答题。",
            "usage": {"input_tokens": 1400, "output_tokens": 280, "cost_usd": 0.0022},
        },
        "PDF-001": {
            "final_answer": "报名时间为 2025-10-05，考试时间 2025-12-20。",
            "usage": {"input_tokens": 2200, "output_tokens": 440, "cost_usd": 0.0039},
        },
        "PDF-002": {
            "final_answer": "表格已结构化提取。",
            "files": {"result.json": {"rows": 24, "columns": ["专业", "人数", "分数"]}},
            "usage": {"input_tokens": 2600, "output_tokens": 520, "cost_usd": 0.0047},
        },
        "PLAN-001": {
            "final_answer": "6 个月复习计划分三阶段：基础、强化、冲刺，每周任务如下。",
            "usage": {"input_tokens": 1300, "output_tokens": 260, "cost_usd": 0.0021},
        },
        "RESEARCH-001": {
            "final_answer": "研究结论：两校在招生规模与专业课难度上存在差异，对比后建议……",
            "sources": [
                src(edu, "示例院校 A 招生目录"),
                src(chsi, "研招网对比数据"),
                src(moe, "教育部招生管理规定"),
            ],
            "citations": [
                cit("cit1", "A 校招生规模", edu),
                cit("cit2", "两校对比", chsi),
                cit("cit3", "管理规定依据", moe),
            ],
            "usage": {"input_tokens": 9800, "output_tokens": 2100, "cost_usd": 0.0210},
            "tool_calls": [
                {"name": "web_search", "ok": True, "arguments": {"q": "招生对比"}},
                {"name": "fetch_page", "ok": True, "arguments": {"url": edu}},
                {"name": "fetch_page", "ok": False, "arguments": {"url": blog},
                 "error": "HTTP 404"},
            ],
        },
        "HAL-001": {
            "final_answer": "该数据未公开，无法确认具体人数，建议以官方公告为准。",
            "usage": {"input_tokens": 700, "output_tokens": 140, "cost_usd": 0.0011},
        },
    }


def _mock_yaml() -> str:
    """手写 YAML（受限子集：缩进映射 / 序列 / 流式 {} []），避免依赖 pyyaml。"""
    preset = _presets()
    lines: list[str] = [
        "# demo：由 tools/gen_demo_benchmark.py 生成的 mock 预设答案集（副本内文件）",
        "name: mock",
        "type: mock",
        "version: \"1.0.0\"",
        "model: mock-model",
        "provider: mock",
        "",
        "parse:",
        "  format: json",
        "  answer_field: final_answer",
        "  toolcalls_field: tool_calls",
        "  sources_field: sources",
        "  citations_field: citations",
        "  usage_field: usage",
        "",
        "params:",
        "  answers_file: \"\"",
        "",
        "answers:",
    ]
    for task_id, payload in preset.items():
        lines.append(f"  {task_id}:")
        for key, value in payload.items():
            lines.append(f"    {key}: {_inline(value)}")
    lines.append("  \"*\":")
    lines.append("    final_answer: \"（mock 默认答案）未登记预设内容。\"")
    lines.append("    usage_source: none")
    lines.append("")
    return "\n".join(lines)


def _inline(value: Any) -> str:
    """把 Python 值渲染成 mini_yaml 支持的**单行**流式写法。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def build(out_dir: Path, *, force: bool = False) -> Path:
    """把仓库复制到 ``out_dir`` 并注入演示任务 / suite / mock 预设。"""
    out_dir = out_dir.resolve()
    if out_dir.exists():
        if not force:
            raise SystemExit(f"目标目录已存在：{out_dir}（加 --force 覆盖）")
        shutil.rmtree(out_dir)
    shutil.copytree(
        REPO_ROOT,
        out_dir,
        ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.pyc", ".pytest_cache", "reports", "results", "*.egg-info"
        ),
    )

    for spec in TASKS:
        directory = (
            out_dir / "benchmark" / "tasks" / "public" / spec["category"] / spec["task_id"]
        )
        directory.mkdir(parents=True, exist_ok=True)
        task = {
            "task_id": spec["task_id"],
            "category": spec["category"],
            "difficulty": spec["difficulty"],
            "instruction": spec["instruction"],
            "network": "offline",
            "time_limit": 120,
            "tool_limit": 30,
            "tags": ["demo", "smoke"],
            "expected": {
                "must_find": spec.get("points", []),
                "must_not_claim": spec.get("must_not_claim", []),
                "required_sources": [],
                "ground_truth": None,
            },
            "grader": {"type": "deterministic", "checks": spec["checks"]},
        }
        (directory / "task.json").write_text(
            json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (directory / "README.md").write_text(
            f"# {spec['task_id']}\n\n演示任务（{spec['category']} / {spec['difficulty']}），"
            "由 `tools/gen_demo_benchmark.py` 生成于项目副本中，用于报告渲染验收。\n",
            encoding="utf-8",
        )

    (out_dir / "benchmark" / "suites" / "demo.yaml").write_text(DEMO_SUITE, encoding="utf-8")
    (out_dir / "config" / "agents" / "mock.yaml").write_text(_mock_yaml(), encoding="utf-8")
    return out_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=DEFAULT_OUT, help="副本输出目录（默认：系统临时目录/kaoyanbench-demo）")
    parser.add_argument("--force", action="store_true", help="目录已存在时覆盖")
    args = parser.parse_args(argv)

    out = build(Path(args.out), force=args.force)
    print(f"演示项目已生成：{out}")
    print(f"  任务数：{len(TASKS)}（8 类，全离线确定性）")
    print(f"  suite：{out / 'benchmark' / 'suites' / 'demo.yaml'}")
    print(f"  mock 预设：{out / 'config' / 'agents' / 'mock.yaml'}")
    print()
    print("下一步：")
    print(f"  cd {out}")
    print("  kaoyanbench --root . run --suite demo --agent mock --tag v1 --seed 42")
    print("  kaoyanbench --root . report --suite demo --agent mock --tag v1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
