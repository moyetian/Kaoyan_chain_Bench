"""QA 报告（秦戈）后端缺陷的回归测试。

覆盖：
- P0-1  ``grade --run-id``：正常路径 / ``--json`` 路径 / 不存在 run_id 错误路径。
- P1-1  ``validate`` 遇「不可加载任务」不再提前中断，其余任务错误照常汇总。
- P1-3  ``fixtures check`` 子命令存在（与 ``fixtures build --check`` 等价）。
- P2-1  ``--json`` 可作子命令参数（``list/run/compare/regression/validate --json``）。
- P2-2  fixture 校验错误不重复输出。
- P1-2  支撑能力：``validate`` 对「判定词原样出现在 instruction」给出 warn。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaoyanbench import cli

from conftest import make_task, write_task


# --------------------------------------------------------------------------- #
# 公共：跑一次 mock run 并拿到确定性 run_id
# --------------------------------------------------------------------------- #
def _run_hal001(project: Path) -> str:
    """写一个可跑通的离线任务并 ``run``，返回确定性 run_id。"""
    write_task(
        project,
        "public",
        make_task(
            "HAL-001",
            category="hallucination",
            instructions="请说明目标院校复试线。",
            checks=[
                {"id": "c1", "type": "string_contains", "dimension": "completeness",
                 "any_of": ["40"]},
            ],
        ),
    )
    rc = cli.main(["--root", str(project), "run", "--agent", "mock",
                   "--task", "HAL-001", "--split", "public", "--seed", "42"])
    assert rc == 0
    return "run_s42_00001"


# --------------------------------------------------------------------------- #
# P0-1 grade --run-id
# --------------------------------------------------------------------------- #
def test_grade_run_id_human_readable_ok(project: Path, capsys) -> None:
    run_id = _run_hal001(project)
    capsys.readouterr()
    rc = cli.main(["--root", str(project), "grade", "--run-id", run_id])
    captured = capsys.readouterr()
    assert rc == 0
    assert f"run_id:  {run_id}" in captured.out
    assert "HAL-001" in captured.out
    # 修复前会抛 AttributeError 并打印该错误；这里必须没有
    assert "AttributeError" not in captured.err
    assert "error_codes" not in captured.err


def test_grade_run_id_json_is_valid(project: Path, capsys) -> None:
    run_id = _run_hal001(project)
    capsys.readouterr()
    rc = cli.main(["--root", str(project), "--json", "grade", "--run-id", run_id])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)  # stdout 必须为纯 JSON
    assert payload["run_id"] == run_id
    assert payload["task_id"] == "HAL-001"
    assert isinstance(payload["error_codes"], list)
    assert "total_score" in payload
    assert payload["grader_mode"] in ("full", "mixed", "degraded")
    # JSON 模式不得把人类可读文本混进 stdout
    assert "run_id:" not in captured.out


def test_grade_run_id_json_via_subcommand_flag(project: Path, capsys) -> None:
    """``grade --run-id X --json``（子命令级 --json）也应输出合法 JSON。"""
    run_id = _run_hal001(project)
    capsys.readouterr()
    rc = cli.main(["--root", str(project), "grade", "--run-id", run_id, "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["run_id"] == run_id


def test_grade_unknown_run_id_is_readable_error(project: Path, capsys) -> None:
    rc = cli.main(["--root", str(project), "grade", "--run-id", "no_such_run_999"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "no_such_run_999" in captured.err
    # 不得崩栈 / 不得泄露裸异常名
    assert "Traceback" not in captured.out + captured.err
    assert "AttributeError" not in captured.err


def test_grade_unknown_run_id_json_no_crash(project: Path, capsys) -> None:
    rc = cli.main(["--root", str(project), "--json", "grade", "--run-id", "no_such_run_999"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "Traceback" not in captured.out + captured.err


# --------------------------------------------------------------------------- #
# P0-1 单测：GradeResult.error_codes 便捷属性
# --------------------------------------------------------------------------- #
def test_grade_result_error_codes_property() -> None:
    from kaoyanbench.core.models import ErrorItem, GradeResult

    g = GradeResult(errors=[
        ErrorItem(code="TIMEOUT"),
        ErrorItem(code="PARSING_FAILURE"),
        ErrorItem(code="TIMEOUT"),  # 重复 → 去重
    ])
    assert g.error_codes == ["TIMEOUT", "PARSING_FAILURE"]
    assert GradeResult().error_codes == []


def test_cli_grade_error_codes_helper_is_defensive() -> None:
    """字段缺失 / 结构异常时 _grade_error_codes 不得抛异常。"""
    from kaoyanbench.cli import _grade_error_codes
    from kaoyanbench.core.models import ErrorItem, GradeResult

    class OnlyErrorCodes:  # 历史实现：只提供 error_codes
        error_codes = ["UNKNOWN"]

    assert _grade_error_codes(GradeResult(errors=[ErrorItem(code="TOOL_FAILURE")])) == ["TOOL_FAILURE"]
    assert _grade_error_codes(OnlyErrorCodes()) == ["UNKNOWN"]
    assert _grade_error_codes(object()) == []


# --------------------------------------------------------------------------- #
# P1-1 validate 遇不可加载任务不中断
# --------------------------------------------------------------------------- #
def _write_raw_task(project: Path, split: str, rel: str, raw: str) -> Path:
    directory = project / "benchmark" / "tasks" / split / rel
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "task.json").write_text(raw, encoding="utf-8")
    return directory


def test_validate_continues_after_unloadable_task(project: Path, capsys) -> None:
    # BAD-001：缺 difficulty（不可加载）→ 修复前会阻断全量扫描
    _write_raw_task(
        project, "public", "search/BAD-001",
        json.dumps({"task_id": "BAD-001", "category": "search",
                    "instruction": "x", "network": "offline"}),
    )
    # BAD-002：difficulty 枚举非法 + time_limit/tool_limit 非正
    _write_raw_task(
        project, "public", "search/BAD-002",
        json.dumps({"task_id": "BAD-002", "category": "search",
                    "difficulty": "impossible", "instruction": "x",
                    "network": "offline", "time_limit": -5, "tool_limit": 0}),
    )

    rc = cli.main(["--root", str(project), "validate"])
    out = capsys.readouterr().out
    assert rc == 2
    # 两条任务的错误都要出现（修复前只有 BAD-001）
    assert "BAD-001" in out
    assert "BAD-002" in out
    assert "缺少必填字段" in out          # BAD-001 的加载错误
    assert "difficulty 不在枚举内" in out  # BAD-002 的 schema 错误
    assert "time_limit 必须为正整数" in out
    # 加载失败错误必须带文件路径（Windows 反斜杠需归一化后断言）
    assert "BAD-001/task.json" in out.replace("\\", "/")


def test_validate_unloadable_task_json_mode(project: Path, capsys) -> None:
    _write_raw_task(
        project, "public", "search/BAD-003",
        "这不是合法 JSON",
    )
    write_task(project, "public", make_task("SEARCH-001", category="search"))
    rc = cli.main(["--json", "--root", str(project), "validate"])
    captured = capsys.readouterr()
    assert rc == 2
    payload = json.loads(captured.out)
    assert payload["error_count"] >= 1
    assert any("BAD-003" in str(i.get("task_id")) for i in payload["issues"])


# --------------------------------------------------------------------------- #
# P1-3 fixtures check 子命令
# --------------------------------------------------------------------------- #
def test_fixtures_check_subcommand_exists(project: Path, capsys) -> None:
    write_task(project, "public", make_task("SEARCH-002", category="search"))
    rc = cli.main(["--root", str(project), "fixtures", "check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "fixtures 校验通过" in out


def test_fixtures_check_equals_build_check(project: Path, capsys) -> None:
    write_task(project, "public", make_task("SEARCH-003", category="search"))
    rc1 = cli.main(["--root", str(project), "fixtures", "check"])
    out1 = capsys.readouterr().out
    rc2 = cli.main(["--root", str(project), "fixtures", "build", "--check"])
    out2 = capsys.readouterr().out
    assert rc1 == rc2 == 0
    assert out1 == out2


def test_fixtures_check_json_mode(project: Path, capsys) -> None:
    write_task(project, "public", make_task("SEARCH-004", category="search"))
    rc = cli.main(["--json", "--root", str(project), "fixtures", "check"])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["check"] is True
    assert payload["ok"] is True


def test_fixtures_check_detects_hash_mismatch(project: Path, capsys) -> None:
    """篡改 fixture 后 fixtures check 必须报 error 且退出 2、且**只报一次**（P2-2）。"""
    import hashlib

    directory = write_task(project, "public", make_task("SEARCH-005", category="search"))
    refs = directory / "references"
    refs.mkdir(exist_ok=True)
    (refs / "data.txt").write_text("原始内容", encoding="utf-8")
    good_hash = hashlib.sha256((refs / "data.txt").read_bytes()).hexdigest()
    (refs / "manifest.json").write_text(
        json.dumps({"files": {"data.txt": {"sha256": good_hash}}}), encoding="utf-8"
    )
    # 篡改 fixture
    (refs / "data.txt").write_text("被篡改内容", encoding="utf-8")

    rc = cli.main(["--root", str(project), "fixtures", "check"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "FAIL  SEARCH-005" in out
    # JSON 模式给出细节，且同一错误只出现一次
    rc2 = cli.main(["--json", "--root", str(project), "fixtures", "check"])
    payload = json.loads(capsys.readouterr().out)
    assert rc2 == 2
    assert payload["ok"] is False
    detail = json.dumps(payload, ensure_ascii=False)
    assert detail.count("fixture hash 不匹配") == 1


# --------------------------------------------------------------------------- #
# P2-1 --json 作为子命令参数
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("subcmd", ["list", "validate", "suite", "agent"])
def test_json_as_subcommand_flag(project: Path, capsys, subcmd: str) -> None:
    write_task(project, "public", make_task("SEARCH-006", category="search"))
    rc = cli.main(["--root", str(project), subcmd, "--json"])
    captured = capsys.readouterr()
    assert rc in (0, 1, 2)
    json.loads(captured.out)  # 必须为合法 JSON（修复前 argparse 直接报错）


def test_json_subcommand_run(project: Path, capsys) -> None:
    write_task(project, "public", make_task("SEARCH-007", category="search"))
    rc = cli.main(["--root", str(project), "run", "--agent", "mock",
                   "--task", "SEARCH-007", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["task_count"] == 1


def test_global_json_still_works(project: Path, capsys) -> None:
    """全局 --json 不能被新加的子命令级 --json 破坏。"""
    write_task(project, "public", make_task("SEARCH-008", category="search"))
    rc = cli.main(["--json", "--root", str(project), "list"])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["task_count"] == 1


# --------------------------------------------------------------------------- #
# P2-2 validate 不重复输出同一 issue
# --------------------------------------------------------------------------- #
def test_validate_does_not_duplicate_fixture_issue(project: Path, capsys) -> None:
    import hashlib

    directory = write_task(project, "public", make_task("SEARCH-009", category="search"))
    refs = directory / "references"
    refs.mkdir(exist_ok=True)
    (refs / "data.txt").write_text("原始内容", encoding="utf-8")
    good_hash = hashlib.sha256((refs / "data.txt").read_bytes()).hexdigest()
    (refs / "manifest.json").write_text(
        json.dumps({"files": {"data.txt": {"sha256": good_hash}}}), encoding="utf-8"
    )
    (refs / "data.txt").write_text("被篡改内容", encoding="utf-8")

    rc = cli.main(["--root", str(project), "validate"])
    out = capsys.readouterr().out
    assert rc == 2
    assert out.count("fixture hash 不匹配") == 1


# --------------------------------------------------------------------------- #
# P1-2 支撑：anti-echo warn（判定词出现在 instruction）
# --------------------------------------------------------------------------- #
def test_validate_warns_on_instruction_keyword_leak(project: Path, capsys) -> None:
    write_task(
        project, "public",
        make_task(
            "HAL-002", category="hallucination",
            instructions="若未找到官方数据，请说明未找到。",
            checks=[{"id": "c1", "type": "string_contains", "dimension": "completeness",
                     "any_of": ["未找到"]}],
        ),
    )
    rc = cli.main(["--root", str(project), "validate"])
    out = capsys.readouterr().out
    assert "判定词" in out
    assert "未找到" in out
    # 仅 warn，不阻断（无 error）
    assert "error=0" in out
    assert rc in (0, 1)


def test_no_keyword_leak_warning_when_absent(project: Path, capsys) -> None:
    write_task(
        project, "public",
        make_task(
            "SEARCH-010", category="search",
            instructions="请根据给定资料回答问题。",
            checks=[{"id": "c1", "type": "string_contains", "dimension": "completeness",
                     "any_of": ["复试线"]}],
        ),
    )
    rc = cli.main(["--root", str(project), "validate"])
    out = capsys.readouterr().out
    assert "判定词" not in out
