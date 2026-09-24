"""CLI 契约测试（方案 5.10）：exit code、参数校验、错误信息可读性。

本文件模拟「测试工程师会重点攻击的地方」：
- 退出码约定：pass→0 / warn→0（--strict-warn→1）/ fail→1 / 运行时错误→2；
- 参数非法（未知命令、缺参、越界）→ 2，且信息可读、**不含堆栈**；
- 错误信息**不得泄露**数据库语句 / 文件路径 / 密钥 / 完整手机号；
- ``--json`` 下 stdout 必须是纯 JSON。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaoyanbench import cli

from conftest import make_task, write_task


# --------------------------------------------------------------------------- #
# 基本信息与帮助
# --------------------------------------------------------------------------- #
def test_version_flag(capsys) -> None:
    # argparse 的 --version 走 SystemExit(0)；版本号与包版本单源一致（勿硬编码）
    import kaoyanbench

    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.strip() == kaoyanbench.__version__


def test_no_command_prints_help(capsys) -> None:
    rc = cli.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "usage:" in out


def test_unknown_command_exits_2(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["definitely-not-a-command"])
    assert exc.value.code == 2


def test_missing_required_option_exits_2(capsys) -> None:
    # report 有一个必需的 --tag 之类？ 用 compare 的必需 tag 触发 argparse 错误
    with pytest.raises(SystemExit) as exc:
        cli.main(["compare"])
    assert exc.value.code == 2


# --------------------------------------------------------------------------- #
# run：找不到任务 → 可读错误 + 退出码 2
# --------------------------------------------------------------------------- #
def test_run_unknown_task_is_readable_error(project: Path, capsys) -> None:
    rc = cli.main(["--root", str(project), "run", "--agent", "mock", "--task", "NOPE-999"])
    captured = capsys.readouterr()
    assert rc == 2
    # 错误信息走 stderr（Console.error）
    assert "NOPE-999" in captured.err
    # 不含裸堆栈
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_run_no_tasks_matched(project: Path, capsys) -> None:
    # 用一个存在的任务 + 不匹配的 category 过滤 → 任务集为空
    write_task(project, "public", make_task("SEARCH-010", category="search"))
    rc = cli.main(["--root", str(project), "run", "--agent", "mock",
                   "--task", "SEARCH-010", "--category", "nope"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "没有匹配" in captured.err or "没有可运行" in captured.err


# --------------------------------------------------------------------------- #
# validate：非法字段 → error 计数、退出码 2（非 strict 时有 error 才 2）
# --------------------------------------------------------------------------- #
def test_validate_bad_enum_exits_2(project: Path, capsys) -> None:
    bad = make_task("SEARCH-004", category="search", difficulty="impossible")
    write_task(project, "public", bad)
    rc = cli.main(["--root", str(project), "validate"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "error=" in out
    assert "difficulty" in out


def test_validate_strict_warn_exits_1(project: Path, capsys) -> None:
    # 一个只有 warn（无 error）的任务：离线 + 无 references + 无 README
    ok = make_task("SEARCH-005", category="search", difficulty="easy")
    write_task(project, "public", ok)
    rc = cli.main(["--root", str(project), "validate", "--strict"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "error=0" in out


# --------------------------------------------------------------------------- #
# JSON 模式纯度
# --------------------------------------------------------------------------- #
def test_validate_json_mode_is_pure(project: Path, capsys) -> None:
    write_task(project, "public", make_task("SEARCH-006", category="search"))
    rc = cli.main(["--json", "--root", str(project), "validate"])
    out = capsys.readouterr().out
    assert rc in (0, 1, 2)
    payload = json.loads(out)  # 必须可解析
    assert "issues" in payload
    assert "error_count" in payload


# --------------------------------------------------------------------------- #
# 错误信息不得泄露敏感内容
# --------------------------------------------------------------------------- #
def test_error_message_no_sensitive_leak(project: Path, capsys) -> None:
    rc = cli.main(["--root", str(project), "run", "--agent", "mock", "--task", "MISSING-1"])
    captured = capsys.readouterr()
    assert rc == 2
    combined = (captured.out + captured.err).lower()
    for bad in ("select ", "insert ", "api_key", "password", "secret"):
        assert bad not in combined


# --------------------------------------------------------------------------- #
# agent / list / suite 子命令冒烟
# --------------------------------------------------------------------------- #
def test_agent_list_smoke(project: Path, capsys) -> None:
    rc = cli.main(["--root", str(project), "agent"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "mock" in out


def test_list_smoke(project: Path, capsys) -> None:
    write_task(project, "public", make_task("SEARCH-007", category="search", tags=["smoke"]))
    rc = cli.main(["--root", str(project), "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "SEARCH-007" in out


def test_suite_list_smoke(project: Path, capsys) -> None:
    rc = cli.main(["--root", str(project), "suite"])
    out = capsys.readouterr().out
    assert rc == 0


# --------------------------------------------------------------------------- #
# BUG-1 回归：--suite 全链路不得抛 TypeError
# --------------------------------------------------------------------------- #
def _write_smoke_suite(project: Path, task_id: str = "SEARCH-007") -> None:
    write_task(project, "public", make_task(
        task_id, category="search", tags=["smoke"],
        checks=[{"id": "c1", "type": "string_contains", "dimension": "factuality",
                 "params": {"any_of": ["招生"]}}],
    ))
    (project / "benchmark" / "suites" / "smoke.yaml").write_text(
        "id: smoke\nsplit: public\nversion: \"1.0\"\ntask_ids: all\n"
        "filters:\n  tags: [smoke]\ndescribe: 冒烟\n",
        encoding="utf-8",
    )


def test_list_with_suite_does_not_raise_typeerror(project: Path, capsys) -> None:
    _write_smoke_suite(project)
    rc = cli.main(["--root", str(project), "list", "--suite", "smoke"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "not iterable" not in (captured.out + captured.err)
    assert "SEARCH-007" in captured.out


def test_validate_with_suite_does_not_raise_typeerror(project: Path, capsys) -> None:
    _write_smoke_suite(project)
    rc = cli.main(["--root", str(project), "validate", "--suite", "smoke"])
    captured = capsys.readouterr()
    assert "not iterable" not in (captured.out + captured.err)
    assert rc == 0


# --------------------------------------------------------------------------- #
# BUG-3 回归：suite.defaults 必须被消费（core50.yaml 声明 runs: 3）
# --------------------------------------------------------------------------- #
def _write_defaults_suite(project: Path, suite_id: str, runs: int) -> None:
    write_task(
        project,
        "public",
        make_task(
            "EXAM-001",
            category="exam",
            checks=[
                {"id": "c1", "type": "string_contains", "dimension": "factuality",
                 "params": {"any_of": ["答案"]}}
            ],
        ),
    )
    (project / "benchmark" / "suites" / f"{suite_id}.yaml").write_text(
        f"id: {suite_id}\nsplit: public\nversion: \"1.0\"\ntask_ids: all\n"
        f"defaults:\n  runs: {runs}\n  time_limit: 120\n  tool_limit: 30\n",
        encoding="utf-8",
    )


def _read_suite_json(project: Path, suite_id: str, agent: str, tag: str) -> dict:
    path = project / "results" / "suites" / f"{suite_id}__{agent}__{tag}.suite.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_run_applies_suite_defaults_runs(project: Path, capsys) -> None:
    """suite.defaults.runs 必须生效（否则 core50 的 Pass@3 静默失效）。"""
    _write_defaults_suite(project, "t3", runs=3)
    rc = cli.main(["--root", str(project), "run", "--suite", "t3", "--agent", "mock", "--tag", "d1"])
    capsys.readouterr()
    assert rc == 0
    data = _read_suite_json(project, "t3", "mock", "d1")
    assert data["runs_per_task"] == 3
    assert len(data["runs"]) == 3


def test_cli_runs_overrides_suite_defaults(project: Path, capsys) -> None:
    """显式 --runs 优先级高于 suite.defaults.runs。"""
    _write_defaults_suite(project, "t3", runs=3)
    rc = cli.main(["--root", str(project), "run", "--suite", "t3", "--agent", "mock",
                   "--tag", "d2", "--runs", "1"])
    capsys.readouterr()
    assert rc == 0
    assert _read_suite_json(project, "t3", "mock", "d2")["runs_per_task"] == 1


# --------------------------------------------------------------------------- #
# QA P3-1 回归：--runs 边界校验（0 不得静默当 1；超大值须拒绝）
# --------------------------------------------------------------------------- #
def test_runs_zero_rejected(project: Path, capsys) -> None:
    """`--runs 0` 原被静默当作 1 次，现须显式报错。"""
    _write_defaults_suite(project, "t3", runs=1)
    rc = cli.main(["--root", str(project), "run", "--suite", "t3", "--agent", "mock",
                   "--tag", "z0", "--runs", "0"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "必须 >= 1" in captured.err


def test_runs_negative_rejected(project: Path, capsys) -> None:
    _write_defaults_suite(project, "t3", runs=1)
    rc = cli.main(["--root", str(project), "run", "--suite", "t3", "--agent", "mock",
                   "--tag", "zn", "--runs", "-5"])
    captured = capsys.readouterr()
    assert rc == 2
    assert ">= 1" in captured.err


def test_runs_over_upper_bound_rejected(project: Path, capsys) -> None:
    """`--runs 999999` 原会无保护启动，现须在启动前拒绝。"""
    _write_defaults_suite(project, "t3", runs=1)
    rc = cli.main(["--root", str(project), "run", "--suite", "t3", "--agent", "mock",
                   "--tag", "zb", "--runs", "999999"])
    captured = capsys.readouterr()
    assert rc == 2
    assert str(cli.MAX_RUNS_PER_TASK) in captured.err
    # 未产生任何结果（校验发生在执行之前）
    assert not (project / "results" / "suites" / "t3__mock__zb.suite.json").exists()


# --------------------------------------------------------------------------- #
# BUG-2 回归：report/regression 不传 --agent 时须按已有结果反查
# --------------------------------------------------------------------------- #
def _set_agent_ref(project: Path, ref: str) -> None:
    path = project / "config" / "default.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("  ref: mock", f"  ref: {ref}"),
        encoding="utf-8",
    )


def test_report_without_agent_resolves_existing_result(project: Path, capsys) -> None:
    """配置的 agent.ref 与实际结果不符时，report 仍应按已有结果反查（README 示例命令）。"""
    _write_defaults_suite(project, "t2", runs=1)
    _set_agent_ref(project, "kaoyan_chain")  # 配置指向未跑过的 agent
    assert cli.main(["--root", str(project), "run", "--suite", "t2",
                     "--agent", "mock", "--tag", "r1"]) == 0
    capsys.readouterr()

    rc = cli.main(["--root", str(project), "report", "--suite", "t2", "--tag", "r1"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "报告目录" in captured.out


def test_regression_without_agent_resolves_existing_result(project: Path, capsys) -> None:
    _write_defaults_suite(project, "t2", runs=1)
    _set_agent_ref(project, "kaoyan_chain")
    for tag in ("b1", "b2"):
        assert cli.main(["--root", str(project), "run", "--suite", "t2",
                         "--agent", "mock", "--tag", tag]) == 0
    capsys.readouterr()

    rc = cli.main(["--root", str(project), "regression", "--suite", "t2",
                   "--baseline", "b1", "--current", "b2"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "task_success_rate" in captured.out
