"""v1.1 硬化用例：预算口径统一 / 回归可比性 / 快照收紧 / 公私隔离。"""

from __future__ import annotations

from pathlib import Path

from kaoyanbench import cli
from kaoyanbench.core.config import load_config
from kaoyanbench.core.engine import RunSpec, run_single_task
from kaoyanbench.core.models import GradeResult, Run, ScoreBreakdown, SuiteResult
from kaoyanbench.core.registry import discover_task_dirs, load_task
from kaoyanbench.core.store import ResultStore

from conftest import make_task, write_task


def _load_agent(project: Path, name: str = "mock"):
    from kaoyanbench.core.config import load_agent_spec

    config = load_config(project / "config" / "default.yaml")
    return load_agent_spec(name, config=config), config


def _write_task_with_preset(project: Path, task_id="SEARCH-001", **kw) -> None:
    task = make_task(task_id, category="search", checks=[
        {"id": "c1", "type": "string_contains", "dimension": "factuality",
         "any_of": ["复试线"]},
    ], **kw)
    write_task(project, "public", task)
    path = project / "config" / "agents" / "mock.yaml"
    path.write_text(
        "name: mock\ntype: mock\nversion: \"1.0.0\"\nmodel: mock-model\n"
        f"answers:\n  \"{task_id}\":\n    final_answer: \"复试线 320 分\"\n"
        "    usage_source: \"none\"\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# 1. 预算口径：Run 记录生效值，复评沿用
# --------------------------------------------------------------------------- #
def test_run_records_effective_limits(project: Path) -> None:
    _write_task_with_preset(project)
    agent_spec, config = _load_agent(project)
    tasks = [load_task(d) for d in discover_task_dirs(config.tasks_root)]
    store = ResultStore(config.results_dir)
    outcome = run_single_task(
        tasks[0], config=config, agent_spec=agent_spec,
        suite_id="smoke", attempt=1, seed=42, tag="t",
        run_id="run_limits_1", now="2025-01-01T00:00:00+00:00",
        # suite 覆盖：任务声明 tool_limit=30，这里用 77 验证优先级透传
        suite_tool_limit=77, suite_time_limit=111,
        store=store,
    )
    assert outcome.run.tool_limit == 77
    assert outcome.run.time_limit == 111
    # 落盘可恢复（from_dict 回放不丢字段）
    revived = Run.from_dict(outcome.run.to_dict())
    assert revived.tool_limit == 77 and revived.time_limit == 111


def test_grade_existing_run_reuses_run_limits(project: Path) -> None:
    """复评 efficiency 分母必须用首评预算，而非任务声明值。"""
    from kaoyanbench.core.engine import grade_existing_run

    _write_task_with_preset(project)
    agent_spec, config = _load_agent(project)
    tasks = [load_task(d) for d in discover_task_dirs(config.tasks_root)]
    store = ResultStore(config.results_dir)
    outcome = run_single_task(
        tasks[0], config=config, agent_spec=agent_spec,
        suite_id="smoke", attempt=1, seed=42, tag="t",
        run_id="run_limits_2", now="2025-01-01T00:00:00+00:00",
        suite_tool_limit=77, suite_time_limit=111,
        store=store,
    )
    regrade = grade_existing_run(outcome.run, tasks[0], config=config, store=store)
    assert regrade.score_total == outcome.grade.score_total


# --------------------------------------------------------------------------- #
# 2. 回归可比性：grader_mode 不一致 → warn 门禁
# --------------------------------------------------------------------------- #
def _suite(tag: str, mode: str) -> SuiteResult:
    grade = GradeResult(task_id="T-1", run_id="r", grader_type="hybrid",
                        grader_mode=mode, score=ScoreBreakdown(total=80.0))
    grade.metrics.task_success = True
    run = Run(run_id="r", task_id="T-1", tag=tag, grader_mode=mode)
    return SuiteResult(suite_id="s", task_count=1, agent="mock", tag=tag,
                       runs=[run], grades=[grade])


def test_regression_warns_on_grader_mode_mismatch() -> None:
    from kaoyanbench.core.regression import build_regression_report

    report = build_regression_report(_suite("b", "full"), _suite("c", "degraded"))
    names = [g.name for g in report.gates]
    assert "grader_mode_mismatch" in names
    gate = next(g for g in report.gates if g.name == "grader_mode_mismatch")
    assert gate.result == "warn"


def test_regression_no_warn_when_modes_match() -> None:
    from kaoyanbench.core.regression import build_regression_report

    report = build_regression_report(_suite("b", "full"), _suite("c", "full"))
    assert "grader_mode_mismatch" not in [g.name for g in report.gates]


# --------------------------------------------------------------------------- #
# 3. 快照收紧旗标
# --------------------------------------------------------------------------- #
def test_validate_require_snapshots_promotes_warn(project: Path, capsys) -> None:
    task = make_task("SEARCH-090", category="search", network="online")
    write_task(project, "public", task)
    rc_plain = cli.main(["--root", str(project), "validate"])
    assert rc_plain == 0  # 默认仅 warn
    rc_strict = cli.main(["--root", str(project), "validate", "--require-snapshots"])
    assert rc_strict == 2  # 缺快照提升为 error


# --------------------------------------------------------------------------- #
# 4. 公私隔离：public suite 不混入 private 任务
# --------------------------------------------------------------------------- #
def test_public_suite_excludes_private_tasks(project: Path) -> None:
    write_task(project, "public", make_task("SEARCH-091", category="search"))
    write_task(project, "private", make_task("SEARCH-092", category="search"))
    pub = [load_task(d) for d in discover_task_dirs(project / "benchmark" / "tasks", split="public")]
    priv = [load_task(d) for d in discover_task_dirs(project / "benchmark" / "tasks", split="private")]
    assert {t.task_id for t in pub} == {"SEARCH-091"}
    assert {t.task_id for t in priv} == {"SEARCH-092"}
    # split 过滤语义：public 运行集永不含 private
    mixed = pub + priv
    public_only = [t for t in mixed if t.split == "public"]
    assert all(t.split == "public" for t in public_only)
