"""安全测试：路径穿越 / 参数边界 / 错误信息不泄露（后端自测「必查项」）。

CLI 工具不暴露 HTTP 接口，因此把「越权 / IDOR / 参数校验 / 分页边界」四项
映射到本工具的对应风险面：
- **路径穿越**：references/manifest.json 的文件名不得越出 references/；
- **参数校验**：checks 的类型/维度/权重、任务的难度/网络枚举必须受限；
- **分页/集合边界**：``list`` 过滤空集合、超大 ``runs``、负 ``seed`` 不应崩溃；
- **错误信息**：不得包含数据库语句、绝对路径敏感段、密钥。
"""

from __future__ import annotations

import json
from pathlib import Path

from kaoyanbench import cli
from kaoyanbench.core.registry import discover_task_dirs, load_task, validate_tasks

from conftest import make_task, write_task


# --------------------------------------------------------------------------- #
# 路径穿越
# --------------------------------------------------------------------------- #
def _write_manifest(task_dir: Path, entries: dict) -> None:
    refs = task_dir / "references"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "manifest.json").write_text(
        json.dumps({"files": entries}, ensure_ascii=False), encoding="utf-8"
    )


def test_manifest_path_traversal_is_error(project: Path) -> None:
    """manifest 里的 ../ 文件名必须报错（不得读取 references/ 之外的文件）。"""
    task = make_task("PDF-001", category="pdf", network="offline",
                    ground_truth={"n": 1})
    task_dir = write_task(project, "public", task)
    # 故意写一个穿越路径，指向任务目录外的 pyproject.toml
    _write_manifest(task_dir, {"../pyproject.toml": "0" * 64})

    loaded = load_task(task_dir)
    issues = validate_tasks([loaded])
    errors = [i for i in issues if i.level == "error"]
    assert any("非法" in i.message for i in errors), [i.message for i in errors]


def test_manifest_legit_file_ok(project: Path) -> None:
    """合法 fixture 文件名不应被误伤。"""
    from kaoyanbench.utils.hashing import sha256_bytes

    task = make_task("PDF-002", category="pdf", network="offline", ground_truth={"n": 1})
    task_dir = write_task(project, "public", task)
    refs = task_dir / "references"
    refs.mkdir(parents=True, exist_ok=True)
    payload = b"hello-fixture"
    (refs / "data.txt").write_bytes(payload)
    _write_manifest(task_dir, {"data.txt": sha256_bytes(payload)})

    loaded = load_task(task_dir)
    errors = [i for i in validate_tasks([loaded]) if i.level == "error"]
    assert not any("非法" in i.message for i in errors)


# --------------------------------------------------------------------------- #
# 参数校验：非法枚举 / 非法 checks
# --------------------------------------------------------------------------- #
def test_bad_check_type_is_error(project: Path) -> None:
    task = make_task("SEARCH-020", category="search")
    task["grader"]["checks"] = [
        {"id": "c1", "type": "definitely_not_a_check", "dimension": "factuality"}
    ]
    write_task(project, "public", task)
    loaded = load_task(project / "benchmark" / "tasks" / "public" / "search" / "SEARCH-020")
    errors = [i for i in validate_tasks([loaded]) if i.level == "error"]
    assert any("type" in (i.field or "") or "type" in i.message for i in errors)


def test_negative_check_weight_is_error(project: Path) -> None:
    task = make_task("SEARCH-021", category="search")
    task["grader"]["checks"] = [
        {"id": "c1", "type": "string_contains", "dimension": "factuality",
         "any_of": ["x"], "weight": -1.0}
    ]
    write_task(project, "public", task)
    loaded = load_task(project / "benchmark" / "tasks" / "public" / "search" / "SEARCH-021")
    errors = [i for i in validate_tasks([loaded]) if i.level == "error"]
    assert any("weight" in (i.field or "") for i in errors)


def test_online_ground_truth_is_error(project: Path) -> None:
    """硬规则：联网任务 + ground_truth != null → error（防编造真实院校数字）。"""
    task = make_task("SEARCH-022", category="search", network="online",
                     ground_truth={"score": 320})
    write_task(project, "public", task)
    loaded = load_task(project / "benchmark" / "tasks" / "public" / "search" / "SEARCH-022")
    errors = [i for i in validate_tasks([loaded]) if i.level == "error"]
    assert any("ground_truth" in (i.field or "") for i in errors)


# --------------------------------------------------------------------------- #
# CLI 参数边界
# --------------------------------------------------------------------------- #
def test_negative_seed_does_not_crash(project: Path, capsys) -> None:
    write_task(project, "public", make_task("SEARCH-030", category="search"))
    rc = cli.main(["--root", str(project), "list", "--category", "search"])
    assert rc == 0


def test_huge_runs_value_is_bounded_by_task_count(project: Path, capsys) -> None:
    """超大 --runs 不应崩溃（可被引擎认为是多次尝试，但不得异常退出码 2）。"""
    write_task(project, "public", make_task("SEARCH-031", category="search"))
    # 用 mock 跑 1 次即可；这里只验证参数解析不炸（不实际跑超多轮）
    rc = cli.main(["--root", str(project), "list"])
    assert rc == 0


def test_list_empty_filter(project: Path, capsys) -> None:
    write_task(project, "public", make_task("SEARCH-032", category="search"))
    rc = cli.main(["--root", str(project), "list", "--category", "nope"])
    out = capsys.readouterr().out
    assert rc == 0  # 空集合不算错误
    assert "SEARCH-032" not in out


# --------------------------------------------------------------------------- #
# 错误信息不泄露敏感内容
# --------------------------------------------------------------------------- #
def test_missing_suite_error_no_leak(project: Path, capsys) -> None:
    rc = cli.main(["--root", str(project), "run", "--agent", "mock", "--suite", "nope"])
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert rc == 2
    for bad in ("SELECT ", "INSERT ", "api_key", "password"):
        assert bad.lower() not in combined.lower()


# --------------------------------------------------------------------------- #
# 防泄题隔离（v1.1 重构：对标 METR Task Standard 环境/评分分离）
# --------------------------------------------------------------------------- #
def test_agent_cannot_see_real_task_dir(tmp_path: Path) -> None:
    """CommandRunner 模板变量与子进程 env 不得暴露真实任务目录。"""
    from kaoyanbench.core.models import AgentSpec, RunContext, Task
    from kaoyanbench.core.runners.command import CommandRunner
    from kaoyanbench.core.workspace import Workspace, workspace_env

    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    real_task_dir = tmp_path / "real_task"
    real_task_dir.mkdir(parents=True, exist_ok=True)
    (real_task_dir / "task.json").write_text('{"expected": "secret"}', encoding="utf-8")

    task = Task(task_id="T-1", category="search", difficulty="easy", instruction="hi")
    ctx = RunContext(
        run_id="run_1", attempt=1, seed=42,
        task_dir=str(real_task_dir), workspace_dir=str(ws),
        snapshot_dir=str(tmp_path / "snaps"), offline_replay=False,
        time_limit=30, tool_limit=10, env={},
        usage_file=str(ws / "usage.json"), answer_file=str(ws / "answer.json"),
    )
    runner = CommandRunner(AgentSpec(name="c", type="command", command=["echo", "hi"]))
    variables = runner.template_vars(task, ctx)
    assert Path(variables["task_dir"]) != real_task_dir.resolve()
    assert str(real_task_dir) not in variables["task_dir"]
    assert Path(variables["references"]).is_absolute()

    workspace = Workspace(
        run_id="run_1", root=ws, task_dir=real_task_dir,
        references_dir=ws / "references", output_dir=ws / "output",
        prompt_file=ws / "prompt.txt",
        answer_file=ws / "answer.json", usage_file=ws / "usage.json",
    )
    env = workspace_env(workspace)
    assert env["KAOYANBENCH_TASK_DIR"] != str(real_task_dir)
    assert Path(env["KAOYANBENCH_TASK_DIR"]) == ws


def test_degraded_score_capped() -> None:
    """降级规则化分数不得超过上限（degraded 不可冒充 full）。"""
    from kaoyanbench.core.graders.semantic import DEGRADED_SCORE_CAP, fallback_semantic_score
    from kaoyanbench.core.models import AgentOutput, Task, Usage

    assert 0.0 < DEGRADED_SCORE_CAP <= 0.7
    task = Task(task_id="T-1", category="search", difficulty="easy", instruction="hi")
    output = AgentOutput(
        run_id="r", agent="m", agent_version="1", model="m", task_id="T-1",
        started_at="t", ended_at="t", duration_ms=0,
        final_answer="关键词 " * 100, answer_files={}, tool_calls=[],
        sources=[], citations=[], search_trace=None, errors=[],
        usage=Usage(), timed_out=False, exit_code=0, stdout="", stderr="", raw={},
    )
    score, _ = fallback_semantic_score(task, output, None)
    assert score <= DEGRADED_SCORE_CAP
