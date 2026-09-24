"""B-01：数据模型往返一致性（``from_dict(to_dict(x)) == x``）。"""

from __future__ import annotations

import dataclasses as dc

import pytest

from kaoyanbench.core import models as M


def _sample_task() -> M.Task:
    return M.Task(
        task_id="SEARCH-001",
        category="search",
        difficulty="easy",
        instruction="查询示例院校 2025 年招生信息。",
        network="offline",
        time_limit=120,
        tool_limit=30,
        expected=M.Expected(
            must_find=[M.Point(id="p1", any_of=["招生"], all_of=[], dimension="factuality")],
            must_not_claim=["保证录取"],
            required_sources=[M.SourceRequirement(level_min="E4", min_count=1)],
            ground_truth={"x": 1},
        ),
        grader=M.GraderSpec(
            type="deterministic",
            checks=[M.Check(id="c1", type="point_hit", dimension="factuality", params={"point_id": "p1"})],
        ),
        split="public",
        tags=["core"],
        version="1.0",
        title="示例",
    )


def test_task_roundtrip() -> None:
    task = _sample_task()
    assert M.Task.from_dict(task.to_dict()) == task


def test_run_roundtrip() -> None:
    run = M.Run(
        run_id="run_1",
        task_id="SEARCH-001",
        agent="mock",
        agent_version="1.0.0",
        model="mock-model",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:01:00Z",
        duration_ms=1000,
        final_answer="答案",
        tool_calls=2,
        sources=[M.Source(url="https://a.example.edu.cn/x", domain="a.example.edu.cn", title="t")],
        citations=[M.Citation(citation_id="cit1", claim="x", source_ref="s1")],
        usage=M.Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3, usage_source="agent_reported"),
        environment=M.Environment(python="3.11", platform="linux"),
    )
    assert M.Run.from_dict(run.to_dict()) == run


def test_usage_none_defaults() -> None:
    u = M.Usage.none()
    assert u.total_tokens is None
    assert u.usage_source == "none"
    assert u.cost_usd is None


def test_score_breakdown_roundtrip() -> None:
    sb = M.ScoreBreakdown(total=80.0, factuality=24.0, source_quality=16.0)
    assert M.ScoreBreakdown.from_dict(sb.to_dict()) == sb


def test_grade_result_roundtrip() -> None:
    grade = M.GradeResult(
        run_id="run_1",
        task_id="SEARCH-001",
        grader_type="deterministic",
        grader_mode="full",
        score=M.ScoreBreakdown(total=80.0, factuality=24.0),
        checks=[M.CheckResult(id="c1", type="point_hit", dimension="factuality", passed=True, detail="ok")],
        metrics=M.Metrics(factuality=0.8),
        graded_at="2026-01-01T00:00:00Z",
    )
    assert M.GradeResult.from_dict(grade.to_dict()) == grade


def test_suite_result_roundtrip() -> None:
    sr = M.SuiteResult(
        suite_id="smoke",
        split="public",
        task_count=1,
        agent="mock",
        agent_version="1.0.0",
        model="mock-model",
        runs_per_task=1,
        seed=42,
        tag="v1",
        created_at="2026-01-01T00:00:00Z",
        environment=M.Environment(python="3.11", platform="linux"),
        aggregates=M.Aggregates(n_tasks=1, task_success_rate=1.0),
        score_breakdown_mean={"total": 80.0},
    )
    assert M.SuiteResult.from_dict(sr.to_dict()) == sr


def test_regression_report_roundtrip() -> None:
    rep = M.RegressionReport(
        baseline_tag="v1",
        current_tag="v2",
        suite_id="smoke",
        compared_at="2026-01-01T00:00:00Z",
        n_tasks_compared=1,
        deltas=[M.MetricDelta(metric="task_success_rate", baseline=0.7, current=0.8, delta_pp=10.0, direction="up")],
        gates=[M.GateResult(name="task_success", metric="task_success_rate", result="pass", delta_pp=10.0)],
        verdict="pass",
        exit_code=0,
    )
    assert M.RegressionReport.from_dict(rep.to_dict()) == rep


def test_validation_issue_dict_stable() -> None:
    issue = M.ValidationIssue("error", "msg", "T-1", "field", "file")
    d = issue.to_dict()
    assert d["level"] == "error"
    assert d["task_id"] == "T-1"


@pytest.mark.parametrize("dim", M.DIMENSIONS)
def test_dimension_max_defined(dim: str) -> None:
    assert dim in M.DIMENSION_MAX


def test_dimension_max_sums_100() -> None:
    # 7 维满分之和应为 100（100 分制）
    assert abs(sum(M.DIMENSION_MAX.values()) - 100.0) < 1e-9


def test_default_weights_sum_one() -> None:
    assert abs(sum(M.DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9


def test_to_dict_keeps_nulls() -> None:
    run = M.Run(run_id="r", task_id="T", agent="a", model="m", started_at="", ended_at="")
    d = run.to_dict()
    assert "final_answer" in d  # key 稳定，不因空值省略


def test_parse_gate_config() -> None:
    gates = M.parse_gate_config(
        {
            "mode": "absolute",
            "task_success_rate": {"max_delta_pp": -5.0, "level": "fail"},
            "latency_seconds_p90": {"max_ratio": 1.5, "level": "warn"},
        }
    )
    assert gates["mode"] == "absolute"
    assert gates["metrics"]["task_success_rate"]["level"] == "fail"
    assert gates["metrics"]["latency_seconds_p90"]["max_ratio"] == 1.5


def test_run_is_failed() -> None:
    run = M.Run(run_id="r", task_id="T", agent="a", model="m", started_at="", ended_at="", timed_out=True)
    assert M.run_is_failed(run, None) is True
    ok_run = M.Run(run_id="r2", task_id="T", agent="a", model="m", started_at="", ended_at="", success=True)
    assert M.run_is_failed(ok_run, None) is False


# --------------------------------------------------------------------------- #
# BUG-2 回归：AgentSpec.from_dict 的 params 不得双层嵌套
# --------------------------------------------------------------------------- #
def test_agent_spec_params_block_is_flat() -> None:
    """BUG-2：``params:`` 块曾被整体塞进 extra → spec.params == {'params': {...}}，
    导致 ``spec.params['answers_file']`` 读不到。修复后必须扁平展开。"""
    spec = M.AgentSpec.from_dict(
        {"name": "mock", "type": "mock", "params": {"answers_file": "/data/a.json", "model": "m1"}}
    )
    assert "params" not in spec.params, f"出现双层嵌套：{spec.params}"
    assert spec.params["answers_file"] == "/data/a.json"
    assert spec.params["model"] == "m1"


def test_agent_spec_answers_top_level_and_in_params_are_equivalent() -> None:
    """两种写法：顶层 ``answers:`` 与 ``params.answers`` 必须取到同一份值。"""
    preset = {"T1": {"final_answer": "ok"}, "T2": {"final_answer": "42"}}
    top = M.AgentSpec.from_dict({"name": "mock", "answers": preset})
    nested = M.AgentSpec.from_dict({"name": "mock", "params": {"answers": preset, "answers_file": ""}})
    assert top.answers == nested.answers == preset
    # nested 写法下 answers 被提升，params 里不再残留 answers
    assert "answers" not in nested.params
    assert nested.params["answers_file"] == ""
