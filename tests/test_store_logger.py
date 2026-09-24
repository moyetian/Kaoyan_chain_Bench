"""B-19：JSONL ↔ SQLite 幂等同步 + 重建；B-10：JSONL 流式写入与脱敏。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaoyanbench.core.logger import (
    EventType,
    RunLogger,
    is_complete_jsonl,
    read_jsonl,
    redact,
    truncate_tool_result,
)
from kaoyanbench.core.models import (
    Environment,
    GradeResult,
    Metrics,
    Run,
    ScoreBreakdown,
)
from kaoyanbench.core.store import ResultStore


def _mk_run(run_id: str, task_id: str = "T-1") -> Run:
    return Run(
        run_id=run_id,
        task_id=task_id,
        agent="mock",
        agent_version="1.0.0",
        model="mock-model",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
        duration_ms=1000,
        final_answer="答案",
        success=True,
        environment=Environment(),
    )


def _mk_grade(run_id: str, task_id: str = "T-1") -> GradeResult:
    return GradeResult(
        task_id=task_id,
        run_id=run_id,
        score=ScoreBreakdown(total=80.0),
        metrics=Metrics(task_success=True),
    )


def test_write_and_read_run(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    run = _mk_run("run_1")
    store.write_run(run)
    back = store.read_run("run_1")
    assert back is not None
    assert back.task_id == "T-1"


def test_write_and_read_grade(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    store.write_grade(_mk_grade("run_1"))
    back = store.read_grade("run_1")
    assert back is not None
    assert back.score.total == 80.0


def test_sync_db_idempotent(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    store.write_run(_mk_run("run_1"))
    store.write_run(_mk_run("run_2"))
    store.write_grade(_mk_grade("run_1"))
    store.write_grade(_mk_grade("run_2"))

    store.sync_db()
    counts1 = store.db_counts()
    store.sync_db()  # 第二次同步
    counts2 = store.db_counts()
    assert counts1 == counts2, "同一 JSONL 同步两次，DB 行数必须不变"
    assert counts1.get("runs", 0) == 2


def test_rm_db_rebuild(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    store.write_run(_mk_run("run_1"))
    store.write_run(_mk_run("run_2"))
    store.sync_db()
    before = store.db_counts()

    # 删掉 DB 后重建
    db_path = store.db_path if hasattr(store, "db_path") else tmp_path / "benchmark.db"
    if db_path.exists():
        db_path.unlink()
    store.rebuild_db()
    after = store.db_counts()
    assert after == before, "rm DB 后应能完整重建"


def test_sync_db_skips_unchanged_files(tmp_path: Path, monkeypatch) -> None:
    """增量同步：未变化的 JSONL 不再重读（避免每次查询都全量 O(n) 读盘）。"""
    import kaoyanbench.core.store as store_mod

    store = ResultStore(tmp_path)
    store.write_run(_mk_run("run_1", "A-1"))
    store.write_run(_mk_run("run_2", "B-1"))

    calls = {"n": 0}
    real = store_mod.read_jsonl

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(store_mod, "read_jsonl", counting)

    assert store.sync_db() == 2
    assert calls["n"] == 2, "首次同步应读取全部 JSONL"

    calls["n"] = 0
    assert store.sync_db() == 0
    assert calls["n"] == 0, "未变化的 JSONL 不应被重读"


def test_sync_db_picks_up_appended_grade(tmp_path: Path) -> None:
    """``write_grade`` 追加到同一 JSONL 后，增量同步仍必须把评分写进 DB。"""
    store = ResultStore(tmp_path)
    store.write_run(_mk_run("run_1", "A-1"))
    store.sync_db()

    store.write_grade(_mk_grade("run_1", "A-1"))
    assert store.sync_db() == 0, "run 已入库，imported 计数不应增加"

    rows = store.query_grades(task_id="A-1", sync=False)
    assert rows, "追加的 grade 必须被增量同步进 DB"


def test_query_runs_by_task(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    store.write_run(_mk_run("run_1", "A-1"))
    store.write_run(_mk_run("run_2", "B-1"))
    runs = store.query_runs(task_id="A-1")
    assert len(runs) == 1
    assert runs[0].task_id == "A-1"


def test_write_and_load_suite(tmp_path: Path) -> None:
    from kaoyanbench.core.models import Aggregates, SuiteResult

    store = ResultStore(tmp_path)
    sr = SuiteResult(
        suite_id="smoke",
        split="public",
        task_count=2,
        agent="mock",
        agent_version="1.0.0",
        model="mock-model",
        runs_per_task=1,
        seed=42,
        tag="v1",
        created_at="2026-01-01T00:00:00Z",
        environment=Environment(),
        aggregates=Aggregates(n_tasks=2, task_success_rate=1.0),
    )
    store.write_suite(sr, "v1")
    back = store.load_suite("smoke", "mock", "v1")
    assert back.suite_id == "smoke"
    assert back.task_count == 2


# --------------------------------------------------------------------------- #
# resolve_suite_ref：报告类命令的 (agent, tag) 反查
# --------------------------------------------------------------------------- #
def _mk_suite(store: ResultStore, suite_id: str, agent: str, tag: str, created_at: str) -> None:
    from kaoyanbench.core.models import Aggregates, SuiteResult

    store.write_suite(
        SuiteResult(
            suite_id=suite_id,
            split="public",
            task_count=1,
            agent=agent,
            agent_version="1.0.0",
            model="m",
            runs_per_task=1,
            seed=42,
            tag=tag,
            created_at=created_at,
            environment=Environment(),
            aggregates=Aggregates(n_tasks=1, task_success_rate=1.0),
        ),
        tag,
    )


def test_resolve_suite_ref_explicit(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    _mk_suite(store, "smoke", "mock", "v1", "2026-01-01T00:00:00Z")
    assert store.resolve_suite_ref("smoke", "mock", "v1") == ("mock", "v1")


def test_resolve_suite_ref_by_tag_only(tmp_path: Path) -> None:
    """只给 tag 时反查唯一 agent（README 的 ``report --suite X --tag Y`` 依赖此行为）。"""
    store = ResultStore(tmp_path)
    _mk_suite(store, "smoke", "mock", "smoke-1", "2026-01-01T00:00:00Z")
    assert store.resolve_suite_ref("smoke", None, "smoke-1") == ("mock", "smoke-1")


def test_resolve_suite_ref_latest_tag(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    _mk_suite(store, "smoke", "mock", "old", "2026-01-01T00:00:00Z")
    _mk_suite(store, "smoke", "mock", "new", "2026-02-01T00:00:00Z")
    assert store.resolve_suite_ref("smoke", "mock", None) == ("mock", "new")
    assert store.resolve_suite_ref("smoke", None, None) == ("mock", "new")


def test_resolve_suite_ref_ambiguous_agent_raises(tmp_path: Path) -> None:
    from kaoyanbench.core.errors import StoreError

    store = ResultStore(tmp_path)
    _mk_suite(store, "smoke", "mock", "v1", "2026-01-01T00:00:00Z")
    _mk_suite(store, "smoke", "kaoyan_chain", "v1", "2026-01-01T00:00:00Z")
    # 同名 tag 下有两个 agent → 拒绝猜测，要求显式 --agent
    with pytest.raises(StoreError):
        store.resolve_suite_ref("smoke", None, "v1")
    assert store.resolve_suite_ref("smoke", "kaoyan_chain", "v1") == ("kaoyan_chain", "v1")


def test_resolve_suite_ref_missing_raises(tmp_path: Path) -> None:
    from kaoyanbench.core.errors import StoreError

    store = ResultStore(tmp_path)
    with pytest.raises(StoreError):
        store.resolve_suite_ref("smoke", None, None)
    _mk_suite(store, "smoke", "mock", "v1", "2026-01-01T00:00:00Z")
    with pytest.raises(StoreError):
        store.resolve_suite_ref("smoke", "mock", "nope")
    with pytest.raises(StoreError):
        store.resolve_suite_ref("smoke", "ghost", "v1")


# --------------------------------------------------------------------------- #
# B-10 logger
# --------------------------------------------------------------------------- #
def test_redact_keys() -> None:
    out = redact({"api_key": "sk-123", "normal": "hello"})
    assert out["api_key"] == "***"
    assert out["normal"] == "hello"


def test_redact_bearer_value() -> None:
    out = redact({"header": "Bearer sk-abcdef123456"})
    assert "sk-abcdef123456" not in str(out)


def test_truncate_tool_result() -> None:
    big = "x" * 10000
    text, digest, truncated = truncate_tool_result(big)
    assert truncated is True
    assert len(text) < len(big)
    assert len(digest) == 64  # sha256 hex


def test_truncate_short_result_not_truncated() -> None:
    text, digest, truncated = truncate_tool_result("short")
    assert truncated is False
    assert text == "short"


def test_run_logger_writes_jsonl(tmp_path: Path) -> None:
    from kaoyanbench.core.models import ToolCall

    log_path = tmp_path / "run.jsonl"
    logger = RunLogger(log_path, run_id="run_1")
    run = _mk_run("run_1")
    logger.run_start(run, task_meta={"category": "search"})
    logger.tool_call(
        ToolCall(call_id="tc1", name="search", arguments={"q": "x"}, ok=True, started_at="t")
    )
    logger.run_end(run)
    records = read_jsonl(log_path)
    assert len(records) == 3
    assert records[0]["event"] == EventType.RUN_START


def test_jsonl_complete(tmp_path: Path) -> None:
    log_path = tmp_path / "run.jsonl"
    logger = RunLogger(log_path, run_id="r")
    run = _mk_run("r")
    logger.run_start(run)
    logger.run_end(run)
    assert is_complete_jsonl(log_path) is True


def test_jsonl_incomplete_without_run_end(tmp_path: Path) -> None:
    log_path = tmp_path / "run.jsonl"
    logger = RunLogger(log_path, run_id="r")
    logger.run_start(_mk_run("r"))
    assert is_complete_jsonl(log_path) is False
