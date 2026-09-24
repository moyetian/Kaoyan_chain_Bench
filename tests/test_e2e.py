"""端到端测试（B-01 / B-23 验收）：任务 → Runner → 日志 → Grader → 聚合 → 报告。

覆盖硬约束 2 的「用临时任务 + mock runner 跑通全链路」要求：
- 引擎层：``RunSpec`` + ``run_suite`` 产出 results JSONL / suite.json / report.json；
- CLI 层：``kaoyanbench run`` / ``report`` / ``validate`` 真实可跑；
- 事实源 ``report.json`` 的 schema 稳定（供前端工程师消费）。

全部任务写在 ``tmp_path``，**不污染 benchmark/tasks/**（硬约束 4）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaoyanbench import cli
from kaoyanbench.core.config import load_config
from kaoyanbench.core.engine import RunSpec, default_run_id_factory, run_suite
from kaoyanbench.core.registry import discover_task_dirs, load_task
from kaoyanbench.core.reporter import build_report, load_report
from kaoyanbench.core.store import ResultStore

from conftest import make_task, write_task


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _mock_agent_yaml(project: Path, task_id: str, *, answer: str) -> None:
    """写一份 mock 预设答案（离线、无模型）。"""
    path = project / "config" / "agents" / "mock.yaml"
    path.write_text(
        "\n".join(
            [
                "name: mock",
                "type: mock",
                'version: "1.0.0"',
                "model: mock-model",
                "provider: mock",
                "parse:",
                "  format: json",
                "  answer_field: final_answer",
                "answers:",
                f'  "{task_id}":',
                f"    final_answer: {json.dumps(answer, ensure_ascii=False)}",
                '    usage_source: "agent_reported"',
                "    usage:",
                "      input_tokens: 128",
                "      output_tokens: 64",
                '  "*":',
                '    final_answer: "兜底"',
                '    usage_source: "none"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_search_task(project: Path) -> Path:
    """写一个「离线、确定性 grader、含 3 个基础检查」的临时任务。"""
    task = make_task(
        "SEARCH-001",
        category="search",
        difficulty="easy",
        instructions="给出复试线，并附官方来源。",
        network="offline",
        tags=["smoke"],
        checks=[
            # 注意：params 平铺（Check.from_dict 的规范形态）
            {"id": "c1", "type": "string_contains", "dimension": "factuality",
             "any_of": ["复试线"]},
            {"id": "c2", "type": "regex", "dimension": "factuality", "pattern": r"20\d{2}"},
            {"id": "c3", "type": "year_tag", "dimension": "factuality", "year": 2025},
        ],
    )
    return write_task(project, "public", task)


def _load_agent(project: Path, name: str = "mock"):
    from kaoyanbench.core.config import load_agent_spec

    config = load_config(project / "config" / "default.yaml")
    return load_agent_spec(name, config=config), config


# --------------------------------------------------------------------------- #
# 引擎层端到端
# --------------------------------------------------------------------------- #
def test_run_suite_writes_results_and_suite_json(project: Path) -> None:
    """run_suite 跑通全链路：JSONL / suite.json / 聚合指标齐备。"""
    _write_search_task(project)
    _mock_agent_yaml(
        project,
        "SEARCH-001",
        answer="根据 2025 年招生简章，复试线为 320 分。",
    )
    agent_spec, config = _load_agent(project)

    tasks = [load_task(d) for d in discover_task_dirs(config.tasks_root)]
    assert [t.task_id for t in tasks] == ["SEARCH-001"]

    store = ResultStore(config.results_dir)
    spec = RunSpec(
        tasks=tasks,
        suite_id="smoke",
        split="public",
        agent_name="mock",
        agent_spec=agent_spec,
        tag="v1.0.0",
        runs_per_task=1,
        seed=42,
    )
    result = run_suite(
        spec,
        config=config,
        store=store,
        agent_spec=agent_spec,
        now="2025-01-01T00:00:00+00:00",
        run_id_factory=lambda now=None: "run_test_00001",
        task_meta={},
    )

    # 没有因为评分异常被吞掉
    assert result.warnings == []
    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.task.task_id == "SEARCH-001"
    assert outcome.grade.grader_mode == "full"
    assert outcome.grade.score_total > 0  # 至少 factuality 命中

    # results/ 落盘
    assert (config.results_dir / "runs" / "run_test_00001.jsonl").is_file()
    assert result.suite_path is not None and result.suite_path.is_file()

    # suite.json schema 稳定
    data = json.loads(result.suite_path.read_text(encoding="utf-8"))
    for key in ("schema_version", "suite_id", "runs", "grades", "aggregates",
                "score_breakdown_mean"):
        assert key in data, f"suite.json 缺少 {key}"
    assert data["aggregates"]["n_tasks"] == 1
    assert data["aggregates"]["task_success_rate"] == 1.0


def test_run_suite_jsonl_has_run_start_and_end(project: Path) -> None:
    """JSONL 是流式事实源：至少含 run_start / run_end 事件。"""
    _write_search_task(project)
    _mock_agent_yaml(project, "SEARCH-001", answer="复试线 320 分，2025 年。")
    agent_spec, config = _load_agent(project)
    tasks = [load_task(d) for d in discover_task_dirs(config.tasks_root)]

    store = ResultStore(config.results_dir)
    run_suite(
        RunSpec(tasks=tasks, suite_id="smoke", agent_name="mock", agent_spec=agent_spec, seed=1),
        config=config,
        store=store,
        agent_spec=agent_spec,
        now="2025-01-01T00:00:00+00:00",
        run_id_factory=lambda now=None: "run_jsonl_1",
        task_meta={},
    )
    lines = [
        json.loads(ln)
        for ln in (config.results_dir / "runs" / "run_jsonl_1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if ln.strip()
    ]
    events = [o.get("event") for o in lines if "event" in o]
    assert "run_start" in events
    assert "run_end" in events


def test_store_idempotent_and_rebuildable(project: Path) -> None:
    """SQLite 是派生数据：重复同步幂等；删库后可从 JSONL 重建。"""
    _write_search_task(project)
    _mock_agent_yaml(project, "SEARCH-001", answer="复试线 320 分，2025 年。")
    agent_spec, config = _load_agent(project)
    tasks = [load_task(d) for d in discover_task_dirs(config.tasks_root)]

    store = ResultStore(config.results_dir)
    run_suite(
        RunSpec(tasks=tasks, suite_id="smoke", agent_name="mock", agent_spec=agent_spec, seed=1),
        config=config,
        store=store,
        agent_spec=agent_spec,
        now="2025-01-01T00:00:00+00:00",
        run_id_factory=lambda now=None: "run_db_1",
        task_meta={},
    )
    store.sync_db()
    counts_1 = store.db_counts()
    store.sync_db()  # 幂等
    counts_2 = store.db_counts()
    assert counts_1 == counts_2
    assert counts_1.get("runs", 0) == 1

    # 删库 → 重建
    db = config.results_dir / "benchmark.db"
    assert db.is_file()
    db.unlink()
    rebuilt = store.rebuild_db()
    assert rebuilt >= 1
    assert store.db_counts().get("runs", 0) == 1


# --------------------------------------------------------------------------- #
# 报告端到端（后端事实源 = JSON）
# --------------------------------------------------------------------------- #
def test_report_json_is_source_of_truth(project: Path, tmp_path: Path) -> None:
    """build_report 先落 report.json；未实现格式只告警不崩。"""
    _write_search_task(project)
    _mock_agent_yaml(project, "SEARCH-001", answer="复试线 320 分，2025 年。")
    agent_spec, config = _load_agent(project)
    tasks = [load_task(d) for d in discover_task_dirs(config.tasks_root)]

    store = ResultStore(config.results_dir)
    result = run_suite(
        RunSpec(tasks=tasks, suite_id="smoke", agent_name="mock", agent_spec=agent_spec, seed=1),
        config=config,
        store=store,
        agent_spec=agent_spec,
        now="2025-01-01T00:00:00+00:00",
        run_id_factory=lambda now=None: "run_rep_1",
        task_meta={},
    )

    out_dir = tmp_path / "report_out"
    paths = build_report(result.suite_result, out_dir, formats=["json", "markdown", "html"])
    assert paths.json is not None and paths.json.is_file()

    # 通过 schema 自检读取
    model = load_report(paths.json)
    payload = model.to_dict()
    for key in ("schema_version", "suite", "run", "summary", "tasks",
                "by_category", "by_difficulty", "score_breakdown_mean", "errors"):
        assert key in payload, f"report.json 缺少 {key}"
    assert payload["suite"]["task_count"] == 1


# --------------------------------------------------------------------------- #
# CLI 端到端
# --------------------------------------------------------------------------- #
def test_cli_run_report_validate(project: Path, capsys) -> None:
    """CLI 三连：run → report → validate 均返回 0。"""
    _write_search_task(project)
    _mock_agent_yaml(project, "SEARCH-001", answer="复试线 320 分，2025 年。")

    base = ["--root", str(project)]
    rc = cli.main(base + ["run", "--agent", "mock", "--task", "SEARCH-001", "--seed", "7"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "SEARCH-001" in out

    # run 的 suite_id 为 "(tasks)"
    rc = cli.main(base + ["report", "--suite", "(tasks)", "--agent", "mock", "--tag", "v1.0.0"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "report.json" in out

    rc = cli.main(base + ["validate"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "error=0" in out


def test_cli_run_json_mode_is_pure_json(project: Path, capsys) -> None:
    """``--json`` 下 stdout 必须是可解析的纯 JSON（供流水线消费）。"""
    _write_search_task(project)
    _mock_agent_yaml(project, "SEARCH-001", answer="复试线 320 分，2025 年。")

    rc = cli.main(["--json", "--root", str(project),
                   "run", "--agent", "mock", "--task", "SEARCH-001", "--seed", "7"])
    captured = capsys.readouterr()
    assert rc == 0, captured.out
    payload = json.loads(captured.out)  # 不应混入日志
    assert payload["suite_id"] == "(tasks)"
    assert payload["task_success_rate"] == 1.0


# --------------------------------------------------------------------------- #
# 可复现性：同 seed 两次运行聚合指标一致
# --------------------------------------------------------------------------- #
def test_reproducible_with_same_seed(project: Path) -> None:
    """固定 seed + 固定 now + 固定 run_id → 两次运行 suite 指标逐字节一致。"""
    _write_search_task(project)
    _mock_agent_yaml(project, "SEARCH-001", answer="复试线 320 分，2025 年。")
    agent_spec, config = _load_agent(project)
    tasks = [load_task(d) for d in discover_task_dirs(config.tasks_root)]

    def _once() -> dict:
        store = ResultStore(config.results_dir)
        result = run_suite(
            RunSpec(tasks=tasks, suite_id="smoke", agent_name="mock",
                    agent_spec=agent_spec, seed=42),
            config=config,
            store=store,
            agent_spec=agent_spec,
            now="2025-01-01T00:00:00+00:00",
            run_id_factory=lambda now=None: "run_fixed",
            task_meta={},
        )
        return result.suite_result.aggregates.to_dict()

    assert _once() == _once()


def test_default_run_id_factory_shape() -> None:
    """默认 run_id 形如 run_<时间戳>_<hex>（注入式，不读系统时钟）。"""
    rid = default_run_id_factory("2025-01-01T12:00:00+00:00")
    assert rid.startswith("run_")
    assert len(rid) > len("run_")


# --------------------------------------------------------------------------- #
# 评分异常兜底：任务不得从结果集中消失
# --------------------------------------------------------------------------- #
def test_zero_check_task_becomes_fail_not_dropped(project: Path) -> None:
    """无任何 check 的任务 → ScoringError 被兜底为 FAIL 结果（而非静默丢弃）。"""
    task = make_task(
        "SEARCH-EMPTY", category="search", difficulty="easy",
        instructions="无 check 的边界任务。", network="offline",
        checks=[],
    )
    write_task(project, "public", task)
    _mock_agent_yaml(project, "SEARCH-EMPTY", answer="随便答")
    agent_spec, config = _load_agent(project)
    tasks = [load_task(d) for d in discover_task_dirs(config.tasks_root)]

    store = ResultStore(config.results_dir)
    result = run_suite(
        RunSpec(tasks=tasks, suite_id="smoke", agent_name="mock",
                agent_spec=agent_spec, seed=1),
        config=config,
        store=store,
        agent_spec=agent_spec,
        now="2025-01-01T00:00:00+00:00",
        run_id_factory=lambda now=None: "run_empty_1",
        task_meta={},
    )
    # 关键：任务仍在 outcomes 中，且被判 FAIL，而不是被 warnings 吞掉
    assert result.warnings == []
    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.grade.grader_mode == "degraded"
    assert outcome.grade.task_success is False
    assert outcome.grade.score_total == 0.0
    assert outcome.grade.degraded_reason


# --------------------------------------------------------------------------- #
# 报告元数据回填回归：cmd_report 必须从任务注册表补全 category/difficulty/network
# --------------------------------------------------------------------------- #
def test_report_backfills_task_meta_not_unknown(project: Path, capsys) -> None:
    """修复前 report.json 的 tasks[].category/difficulty/network 全为 unknown。"""
    _write_search_task(project)
    _mock_agent_yaml(project, "SEARCH-001", answer="复试线 320 分，2025 年。")

    base = ["--root", str(project)]
    rc = cli.main(base + ["run", "--agent", "mock", "--task", "SEARCH-001", "--seed", "7"])
    capsys.readouterr()
    assert rc == 0

    rc = cli.main(base + ["report", "--suite", "(tasks)", "--agent", "mock", "--tag", "v1.0.0"])
    out = capsys.readouterr().out
    assert rc == 0, out

    report_path = project / "reports" / "(tasks)__mock__v1.0.0" / "report.json"
    if not report_path.is_file():
        # 目录名转义可能在实现里不同，退化为搜索
        import glob
        found = glob.glob(str(project / "reports" / "**" / "report.json"), recursive=True)
        assert found, "未找到 report.json"
        report_path = Path(found[0])

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    row = {t["task_id"]: t for t in payload["tasks"]}["SEARCH-001"]
    assert row["category"] == "search", row
    assert row["difficulty"] == "easy", row
    assert row["network"] == "offline", row
