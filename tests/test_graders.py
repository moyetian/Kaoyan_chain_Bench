"""B-13/14/15/16：Deterministic / Semantic / Hybrid / Manual Grader 与降级路径。
B-05：Workspace 隔离 与 Sandbox env 白名单。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaoyanbench.core.config import load_config
from kaoyanbench.core.grader import GradeContext, get_grader, registered_graders
from kaoyanbench.core.graders.deterministic import DeterministicGrader
from kaoyanbench.core.graders.hybrid import HybridGrader
from kaoyanbench.core.graders.manual import load_human_review, write_human_review
from kaoyanbench.core.graders.semantic import (
    SemanticClient,
    build_semantic_client,
    fallback_semantic_score,
)
from kaoyanbench.core.models import (
    AgentOutput,
    Check,
    Environment,
    Expected,
    GraderSpec,
    Point,
    Rubric,
    RubricCriterion,
    Task,
)
from kaoyanbench.core.sandbox import build_sandbox_env, is_sensitive_env
from kaoyanbench.core.workspace import prepare_workspace

REPO_ROOT = Path(__file__).resolve().parents[1]


def _task(grader_type: str = "deterministic", checks=None, rubric=None) -> Task:
    return Task(
        task_id="T-1",
        category="search",
        difficulty="easy",
        instruction="x",
        expected=Expected(must_find=[Point(id="p1", any_of=["招生"], all_of=[])]),
        grader=GraderSpec(type=grader_type, checks=checks or [], rubric=rubric),
    )


def _output(final_answer: str = "今年招生的信息如下") -> AgentOutput:
    return AgentOutput(
        run_id="r1", agent="mock", model="m", task_id="T-1",
        started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:00:01Z",
        duration_ms=1000, final_answer=final_answer, environment=Environment(),
    )


def _ctx(tmp_path: Path, **kw) -> GradeContext:
    return GradeContext(
        task_dir=tmp_path,
        workspace_dir=tmp_path / "ws",
        snapshot_dir=tmp_path / "snaps",
        now="2026-01-01T00:00:00Z",
        reports_dir=kw.get("reports_dir", tmp_path / "reports"),
        write_human_review=kw.get("write_human_review", False),
        semantic_client=kw.get("semantic_client"),
    )


def test_graders_registered() -> None:
    for name in ("deterministic", "semantic", "hybrid", "manual"):
        assert name in registered_graders()


def test_deterministic_grader_stable(tmp_path: Path) -> None:
    task = _task(checks=[Check(id="c1", type="point_hit", dimension="factuality", params={"point_id": "p1"})])
    grader = DeterministicGrader(task.grader)
    g1 = grader.grade(task, _output(), _ctx(tmp_path))
    g2 = grader.grade(task, _output(), _ctx(tmp_path))
    assert g1.to_dict() == g2.to_dict()
    assert g1.grader_mode == "full"


def test_deterministic_grader_fail(tmp_path: Path) -> None:
    task = _task(checks=[Check(id="c1", type="point_hit", dimension="factuality", params={"point_id": "p1"})])
    grader = DeterministicGrader(task.grader)
    g = grader.grade(task, _output("完全无关"), _ctx(tmp_path))
    assert g.metrics.task_success is False
    assert g.score.total < 60.0


def test_semantic_grader_degraded_without_client(tmp_path: Path) -> None:
    rubric = Rubric(prompt_version="v1", criteria=[RubricCriterion(id="c1", dimension="factuality", question="?")])
    task = _task(grader_type="semantic", rubric=rubric)
    grader = get_grader(task)
    g = grader.grade(task, _output(), _ctx(tmp_path, semantic_client=None))
    assert g.grader_mode == "degraded"
    assert g.degraded_reason == "no_api_key"


def test_build_semantic_client_none_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRADER_API_KEY", raising=False)
    cfg = load_config(REPO_ROOT / "config" / "default.yaml", root=REPO_ROOT)
    client = build_semantic_client(cfg)
    assert client is None


def test_hybrid_grader_degraded_without_client(tmp_path: Path) -> None:
    rubric = Rubric(prompt_version="v1", criteria=[RubricCriterion(id="c1", dimension="factuality", question="?")])
    task = _task(
        grader_type="hybrid",
        checks=[Check(id="c1", type="point_hit", dimension="factuality", params={"point_id": "p1"})],
        rubric=rubric,
    )
    grader = HybridGrader(task.grader)
    g = grader.grade(task, _output(), _ctx(tmp_path, semantic_client=None))
    assert g.grader_mode == "degraded"


def test_hybrid_with_stub_client_full(tmp_path: Path) -> None:
    from kaoyanbench.core.graders.semantic import JudgeReply

    class StubClient(SemanticClient):
        def __init__(self) -> None:  # noqa: D401 - 简单 stub
            self.base_url = "stub"
            self.model = "stub-model"

        def available(self) -> bool:  # type: ignore[override]
            return True

        def judge(self, *args, **kwargs):  # type: ignore[override]
            return JudgeReply(supported=True, score=0.8, reason="ok")

    rubric = Rubric(prompt_version="v1", criteria=[RubricCriterion(id="c1", dimension="factuality", question="?")])
    task = _task(
        grader_type="hybrid",
        checks=[Check(id="c1", type="point_hit", dimension="factuality", params={"point_id": "p1"})],
        rubric=rubric,
    )
    grader = HybridGrader(task.grader)
    g = grader.grade(task, _output(), _ctx(tmp_path, semantic_client=StubClient()))
    assert g.grader_mode == "full"


def test_manual_grader_not_scored_and_writes_review(tmp_path: Path) -> None:
    task = _task(
        grader_type="manual",
        checks=[Check(id="c1", type="point_hit", dimension="factuality", params={"point_id": "p1"})],
    )
    grader = get_grader(task)
    reports = tmp_path / "reports"
    reports.mkdir()
    ctx = _ctx(tmp_path, reports_dir=reports, write_human_review=True)
    g = grader.grade(task, _output(), ctx)
    assert g.grader_mode == "degraded"
    review_file = reports / "human_review.jsonl"
    assert review_file.is_file()
    rows = load_human_review(review_file)
    assert len(rows) == 1


def test_fallback_semantic_score_bounded() -> None:
    task = _task()
    score, reason = fallback_semantic_score(task, _output("招生 招生 招生"))
    assert 0.0 <= score <= 1.0


# --------------------------------------------------------------------------- #
# 语义评测重试（方案 5.3：invalid_response 最多重试 1 次）
# --------------------------------------------------------------------------- #
def _client(**kw) -> SemanticClient:
    return SemanticClient("https://x.example/v1", "k", "m", **kw)


def test_semantic_judge_retries_invalid_response(monkeypatch: pytest.MonkeyPatch) -> None:
    from kaoyanbench.core.graders.semantic import JudgeReply

    client = _client(max_retries=1)
    calls = {"n": 0}

    def fake_once(prompt: str) -> JudgeReply:
        calls["n"] += 1
        if calls["n"] == 1:
            return JudgeReply(error="invalid_response")
        return JudgeReply(supported=True, score=1.0, reason="ok")

    monkeypatch.setattr(client, "_judge_once", fake_once)
    reply = client.judge("c", "a")
    assert calls["n"] == 2, "首次 invalid_response 后应重试一次"
    assert reply.error is None and reply.score == 1.0


def test_semantic_judge_retries_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from kaoyanbench.core.graders.semantic import JudgeReply

    client = _client(max_retries=2)
    calls = {"n": 0}

    def fake_once(prompt: str) -> JudgeReply:
        calls["n"] += 1
        return JudgeReply(error="invalid_response")

    monkeypatch.setattr(client, "_judge_once", fake_once)
    assert client.judge("c", "a").error == "invalid_response"
    assert calls["n"] == 3, "1 次原始调用 + 2 次重试后必须停止"


def test_semantic_judge_no_retry_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """网络类错误不重试（避免在无网环境下拖长评测时间，直接走降级）。"""
    from kaoyanbench.core.graders.semantic import JudgeReply

    client = _client(max_retries=1)
    calls = {"n": 0}

    def fake_once(prompt: str) -> JudgeReply:
        calls["n"] += 1
        return JudgeReply(error="network_unreachable")

    monkeypatch.setattr(client, "_judge_once", fake_once)
    assert client.judge("c", "a").error == "network_unreachable"
    assert calls["n"] == 1


def test_semantic_judge_no_key_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SemanticClient("https://x.example/v1", None, "m")
    monkeypatch.setattr(
        client, "_judge_once", lambda _p: pytest.fail("无 api_key 不应发起请求")
    )
    assert client.judge("c", "a").error == "no_api_key"


def test_semantic_client_retries_from_config() -> None:
    from kaoyanbench.core.config import ModelConfig

    cfg = ModelConfig(name="m", base_url="https://x.example/v1", api_key_env="K", max_retries=3)
    assert cfg.to_dict()["max_retries"] == 3
    assert ModelConfig.from_dict({"max_retries": 0}).max_retries == 0
    assert ModelConfig.from_dict({}).max_retries == 1


# --------------------------------------------------------------------------- #
# B-05 workspace / sandbox
# --------------------------------------------------------------------------- #
def test_workspace_distinct_paths(tmp_path: Path) -> None:
    task = _task()
    w1 = prepare_workspace(tmp_path, "run_a", task)
    w2 = prepare_workspace(tmp_path, "run_b", task)
    assert w1.root != w2.root


def test_env_whitelist_blocks_api_key(tmp_path: Path) -> None:
    task = _task()
    ws = prepare_workspace(tmp_path, "run_env", task)
    env = build_sandbox_env(ws, passthrough=[], environ={"OPENAI_API_KEY": "sk-secret", "PATH": "/usr/bin"})
    assert "OPENAI_API_KEY" not in env


def test_env_passthrough_allows_explicit(tmp_path: Path) -> None:
    task = _task()
    ws = prepare_workspace(tmp_path, "run_env2", task)
    with pytest.warns(UserWarning, match="OPENAI_API_KEY"):
        env = build_sandbox_env(
            ws, passthrough=["OPENAI_API_KEY"], environ={"OPENAI_API_KEY": "sk-secret"}
        )
    # 显式声明仍然生效（不拦截），但必须告警
    assert env.get("OPENAI_API_KEY") == "sk-secret"


def test_sensitive_passthrough_warns(tmp_path: Path) -> None:
    """``env_passthrough`` 含敏感变量时必须告警（回归：is_sensitive_env 曾未被调用）。"""
    from kaoyanbench.core.sandbox import sensitive_env_warnings

    messages = sensitive_env_warnings(["OPENAI_API_KEY", "GRADER_API_KEY", "LANG"])
    assert len(messages) == 1
    assert "OPENAI_API_KEY" in messages[0] and "GRADER_API_KEY" in messages[0]
    assert sensitive_env_warnings(["LANG", "PATH"]) == []

    task = _task()
    ws = prepare_workspace(tmp_path, "run_env3", task)
    with pytest.warns(UserWarning):
        build_sandbox_env(ws, passthrough=["GRADER_API_KEY"], environ={"GRADER_API_KEY": "g"})


def test_workspace_home_is_redirected(tmp_path: Path) -> None:
    task = _task()
    ws = prepare_workspace(tmp_path, "run_home", task)
    env = build_sandbox_env(ws, passthrough=[], environ={"PATH": "/usr/bin"})
    assert str(ws.root) in env.get("HOME", "")


def test_is_sensitive_env() -> None:
    assert is_sensitive_env("OPENAI_API_KEY") is True
    assert is_sensitive_env("PATH") is False
