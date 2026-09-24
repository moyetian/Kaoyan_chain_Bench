"""B-06/07/08/09：Echo / Mock / Command / Http Runner 与超时路径。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kaoyanbench.core.models import (
    AgentOutput,
    AgentSpec,
    ParseSpec,
    RunContext,
    Task,
    Usage,
)
from kaoyanbench.core.runner import (
    estimate_usage_from_chars,
    extract_usage,
    get_runner,
    parse_stdout,
    registered_runners,
)
from kaoyanbench.core.runners.command import CommandRunner
from kaoyanbench.core.runners.echo import EchoRunner
from kaoyanbench.core.runners.http import HttpRunner
from kaoyanbench.core.runners.mock import MockRunner


def _task(task_id: str = "T-1") -> Task:
    return Task(task_id=task_id, category="search", difficulty="easy", instruction="请回答。")


def _ctx(tmp_path: Path, **kw) -> RunContext:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    return RunContext(
        run_id=kw.get("run_id", "run_1"),
        attempt=kw.get("attempt", 1),
        seed=kw.get("seed", 42),
        task_dir=str(kw.get("task_dir", tmp_path)),
        workspace_dir=str(ws),
        snapshot_dir=str(tmp_path / "snaps"),
        offline_replay=kw.get("offline_replay", False),
        time_limit=kw.get("time_limit", 30),
        tool_limit=kw.get("tool_limit", 10),
        env=kw.get("env", {}),
        usage_file=str(ws / "usage.json"),
        answer_file=str(ws / "answer.json"),
    )


def test_builtin_runners_registered() -> None:
    for key in ("echo", "mock", "command", "http"):
        assert key in registered_runners()


def test_echo_runner_no_fabrication(tmp_path: Path) -> None:
    runner = EchoRunner(AgentSpec(name="echo", type="echo"))
    out = runner.run(_task(), _ctx(tmp_path))
    assert isinstance(out, AgentOutput)
    assert out.usage.usage_source == "none"
    assert out.tool_calls == []  # AgentOutput.tool_calls 是列表；Echo 不伪造工具调用


def test_mock_runner_preset(tmp_path: Path) -> None:
    spec = AgentSpec(
        name="mock",
        type="mock",
        answers={"T-1": {"final_answer": "预设答案", "usage_source": "none"}},
    )
    runner = MockRunner(spec)
    out = runner.run(_task(), _ctx(tmp_path))
    assert out.final_answer == "预设答案"


def test_mock_runner_default_wildcard(tmp_path: Path) -> None:
    spec = AgentSpec(name="mock", type="mock", answers={"*": {"final_answer": "兜底"}})
    runner = MockRunner(spec)
    out = runner.run(_task("OTHER-1"), _ctx(tmp_path))
    assert out.final_answer == "兜底"


def test_mock_runner_missing_preset_unknown_error(tmp_path: Path) -> None:
    spec = AgentSpec(name="mock", type="mock", answers={})
    runner = MockRunner(spec)
    out = runner.run(_task("X-1"), _ctx(tmp_path))
    assert out.final_answer == ""
    assert any(e.code == "UNKNOWN" for e in out.errors)


def test_command_runner_timeout_kills_process(tmp_path: Path) -> None:
    # 用解释器 sleep 代替系统 sleep 命令（Windows 无 sleep 可执行文件，保证跨平台）
    spec = AgentSpec(
        name="cmd",
        type="command",
        command=[sys.executable, "-c", "import time; time.sleep(999)"],
        stdin="none",
        parse=ParseSpec(format="text"),
    )
    runner = CommandRunner(spec)
    out = runner.run(_task(), _ctx(tmp_path, time_limit=2))
    assert out.timed_out is True
    assert any(e.code == "TIMEOUT" for e in out.errors)


def test_command_runner_echo(tmp_path: Path) -> None:
    # 用当前解释器代替 /bin/echo，保证 Windows/Linux 都能跑
    spec = AgentSpec(
        name="cmd",
        type="command",
        command=[sys.executable, "-c", "print('hello world')"],
        stdin="none",
        parse=ParseSpec(format="text"),
    )
    runner = CommandRunner(spec)
    out = runner.run(_task(), _ctx(tmp_path))
    assert "hello world" in out.final_answer or "hello world" in out.stdout


def test_command_runner_json_parsing(tmp_path: Path) -> None:
    payload = json.dumps({"final_answer": "答案来自 JSON", "usage": {"total_tokens": 10}})
    script = tmp_path / "emit.py"
    script.write_text(f"import sys; sys.stdout.write({payload!r})", encoding="utf-8")
    spec = AgentSpec(
        name="cmd",
        type="command",
        command=[sys.executable, str(script)],
        stdin="none",
        parse=ParseSpec(format="json", answer_field="final_answer", usage_field="usage"),
    )
    runner = CommandRunner(spec)
    out = runner.run(_task(), _ctx(tmp_path))
    assert out.final_answer == "答案来自 JSON"
    assert out.usage.total_tokens == 10


def test_command_runner_parse_failure_falls_back_to_text(tmp_path: Path) -> None:
    script = tmp_path / "bad.py"
    script.write_text("print('not json at all')", encoding="utf-8")
    spec = AgentSpec(
        name="cmd",
        type="command",
        command=[sys.executable, str(script)],
        stdin="none",
        parse=ParseSpec(format="json", answer_field="final_answer", fallbacks=["text"]),
    )
    runner = CommandRunner(spec)
    out = runner.run(_task(), _ctx(tmp_path))
    # 不应崩溃；答案或输出中应含文本内容
    assert "not json at all" in (out.final_answer + out.stdout)


def test_http_runner_no_key_does_not_crash(tmp_path: Path) -> None:
    spec = AgentSpec(name="http", type="http", base_url="", api_key_env="NONEXISTENT_KEY")
    runner = HttpRunner(spec)
    out = runner.run(_task(), _ctx(tmp_path))
    assert out.usage.usage_source == "none"
    assert out.errors  # 明确错误项


# --------------------------------------------------------------------------- #
# stdout 三级解析
# --------------------------------------------------------------------------- #
def test_parse_stdout_json() -> None:
    outcome = parse_stdout('{"final_answer": "x"}', AgentSpec(name="a", type="command", parse=ParseSpec(format="json")))
    assert outcome.data is not None
    assert outcome.data.get("final_answer") == "x"


def test_parse_stdout_text_fallback() -> None:
    outcome = parse_stdout("plain text answer", AgentSpec(name="a", type="command", parse=ParseSpec(format="json", fallbacks=["text"])))
    assert "plain text answer" in (outcome.text or "")


# --------------------------------------------------------------------------- #
# usage 三级回退
# --------------------------------------------------------------------------- #
def test_extract_usage_agent_reported() -> None:
    usage = extract_usage(
        {"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}},
        "usage",
    )
    assert usage.usage_source == "agent_reported"
    assert usage.total_tokens == 3


def test_extract_usage_none_when_missing() -> None:
    usage = extract_usage(None, None, final_answer="")
    assert usage.usage_source == "none"
    assert usage.total_tokens is None  # 禁止填 0


def test_estimate_usage_from_chars() -> None:
    usage = estimate_usage_from_chars("x" * 400)
    assert usage.usage_source == "estimated_from_chars"
    assert usage.estimated is True
    assert usage.total_tokens is not None


def test_get_runner_dispatch() -> None:
    assert isinstance(get_runner(AgentSpec(name="e", type="echo")), EchoRunner)
    assert isinstance(get_runner(AgentSpec(name="m", type="mock")), MockRunner)
    assert isinstance(get_runner(AgentSpec(name="h", type="http")), HttpRunner)
