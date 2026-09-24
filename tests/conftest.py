"""pytest 公共 fixture：在 ``tmp_path`` 里搭一个**最小可跑**的 KaoyanBench 项目。

设计原则（硬约束 4）：
- **不污染 ``benchmark/tasks/``**：全部测试任务写在 pytest 的临时目录里。
- 生成的临时项目自带 ``pyproject.toml`` / ``config/default.yaml`` / 任务目录，
  使 ``find_project_root`` 与 ``load_config`` 都能正确定位。
- 时间/随机全部注入，保证可复现。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


DEFAULT_YAML = """\
benchmark:
  name: KaoyanBench
  version: "1.0"
  tasks_root: benchmark/tasks
  suites_root: benchmark/suites
  default_suite: smoke

agent:
  ref: mock
  version: "1.0.0"

model:
  provider: openai_compatible
  name: test-model
  base_url: ${OPENAI_BASE_URL:-}
  api_key_env: OPENAI_API_KEY
  temperature: 0

grader_model:
  provider: openai_compatible
  name: test-grader-model
  base_url: ${GRADER_BASE_URL:-}
  api_key_env: GRADER_API_KEY
  temperature: 0
  allow_same_model: false

runtime:
  timeout: 120
  timeout_grace_sec: 5
  max_tool_calls: 30
  concurrency: 1
  workspace_root: results/workspaces
  keep_workspace: false
  env_passthrough: []
  docker:
    enabled: false
    image: ""
  fail_fast: false

evaluation:
  runs: 1
  seed: 42
  offline_replay: false
  snapshot_dir: benchmark/snapshots
  grader_type: deterministic
  pass_threshold: 60.0
  weights:
    factuality: 0.30
    source_quality: 0.20
    citation: 0.15
    completeness: 0.15
    tool_execution: 0.10
    task_completion: 0.05
    efficiency: 0.05
  strict_warn: false

gates:
  mode: absolute
  task_success_rate:      { max_delta_pp: -5.0, level: fail }
  citation_accuracy_mean: { max_delta_pp: -5.0, level: fail }
  hallucination_rate_mean:{ max_delta_pp: 3.0, level: fail }
  latency_seconds_p90:    { max_ratio: 1.5, level: warn }

output:
  dir: reports
  format: [json, markdown, html]
  results_dir: results
"""


def make_task(
    task_id: str,
    *,
    category: str,
    difficulty: str = "easy",
    instructions: str = "请回答问题。",
    points: list[dict[str, Any]] | None = None,
    must_not_claim: list[str] | None = None,
    checks: list[dict[str, Any]] | None = None,
    grader_type: str = "deterministic",
    network: str = "offline",
    tags: list[str] | None = None,
    answer_format: dict[str, Any] | None = None,
    required_sources: list[dict[str, Any]] | None = None,
    ground_truth: dict[str, Any] | None = None,
    time_limit: int = 60,
    tool_limit: int = 30,
) -> dict[str, Any]:
    """构造一个 ``task.json`` 的 dict（供 conftest 各测试复用）。"""
    expected: dict[str, Any] = {
        "must_find": points or [],
        "must_not_claim": must_not_claim or [],
        "required_sources": required_sources or [],
        "ground_truth": ground_truth,
    }
    grader: dict[str, Any] = {"type": grader_type, "checks": checks or []}
    if "semantic" in grader_type:
        grader["rubric"] = {
            "prompt_version": "v1",
            "criteria": [
                {"id": "c1", "dimension": "factuality", "question": "事实是否准确？", "weight": 1.0}
            ],
        }
    task: dict[str, Any] = {
        "task_id": task_id,
        "category": category,
        "difficulty": difficulty,
        "instruction": instructions,
        "network": network,
        "time_limit": time_limit,
        "tool_limit": tool_limit,
        "tags": tags or [],
        "expected": expected,
        "grader": grader,
    }
    if answer_format:
        task["answer_format"] = answer_format
    return task


def write_task(root: Path, split: str, task: dict[str, Any]) -> Path:
    """把 task dict 写进 ``<root>/benchmark/tasks/<split>/<category>/<task_id>/task.json``。"""
    directory = root / "benchmark" / "tasks" / split / task["category"] / task["task_id"]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "task.json").write_text(
        json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # README（消除 warn）
    (directory / "README.md").write_text(
        f"# {task['task_id']}\n\n考点与陷阱（测试用临时任务）。\n", encoding="utf-8"
    )
    return directory


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """在 tmp_path 里搭一个最小项目骨架并返回其根目录。"""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='kaoyanbench-test'\nversion='1.0.0'\n", encoding="utf-8"
    )
    (tmp_path / "config" / "agents").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "graders").mkdir(parents=True, exist_ok=True)
    (tmp_path / "benchmark" / "tasks" / "public").mkdir(parents=True, exist_ok=True)
    (tmp_path / "benchmark" / "suites").mkdir(parents=True, exist_ok=True)
    (tmp_path / "benchmark" / "snapshots").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "default.yaml").write_text(DEFAULT_YAML, encoding="utf-8")
    # 复制真实的 agent 卡片（mock/echo），保证 run 可用
    for name in ("mock", "echo"):
        src = REPO_ROOT / "config" / "agents" / f"{name}.yaml"
        if src.is_file():
            (tmp_path / "config" / "agents" / f"{name}.yaml").write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8"
            )
    return tmp_path


@pytest.fixture()
def mock_yaml(project: Path):
    """返回一个可写 mock 预设答案的辅助函数。"""
    path = project / "config" / "agents" / "mock.yaml"

    def _write(answers: dict[str, Any]) -> None:
        lines = [
            "name: mock",
            "type: mock",
            "version: \"1.0.0\"",
            "model: mock-model",
            "answers:",
        ]
        for task_id, preset in answers.items():
            lines.append(f"  \"{task_id}\":")
            lines.append(f"    final_answer: {json.dumps(preset.get('final_answer', ''), ensure_ascii=False)}")
            if "usage" in preset:
                lines.append("    usage:")
                for k, v in preset["usage"].items():
                    lines.append(f"      {k}: {v}")
            if "usage_source" in preset:
                lines.append(f"    usage_source: {preset['usage_source']}")
            if "sources" in preset:
                lines.append("    sources:")
                for s in preset["sources"]:
                    lines.append(f"      - url: {json.dumps(s.get('url',''), ensure_ascii=False)}")
                    lines.append(f"        domain: {json.dumps(s.get('domain',''), ensure_ascii=False)}")
                    lines.append(f"        title: {json.dumps(s.get('title',''), ensure_ascii=False)}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 默认空（`*` 兜底在真实 mock.yaml 中）
    return _write
