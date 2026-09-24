"""P2 用例：控制台编码容错 / 回放来源重判定 / 私测集可解 / 快照覆盖率。"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from kaoyanbench.core.grader import GradeContext, get_grader
from kaoyanbench.core.models import (
    AgentOutput,
    Check,
    Environment,
    Expected,
    GraderSpec,
    Task,
    Usage,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# 1. 控制台编码容错（Windows gbk 下 Emoji 不得抛 UnicodeEncodeError）
# --------------------------------------------------------------------------- #
class _GbkStdout(io.StringIO):
    encoding = "gbk"


def test_console_safe_on_gbk_terminal(monkeypatch) -> None:
    from kaoyanbench.cli import Console, _safe_console_text

    stub = _GbkStdout()
    monkeypatch.setattr(sys, "stdout", stub)
    assert _safe_console_text("validate 通过 ✅") == "validate 通过 [OK]"
    assert _safe_console_text("validate 未通过 ❌") == "validate 未通过 [FAIL]"
    assert "中文保留" in _safe_console_text("中文保留 ✅")
    console = Console(json_mode=False)
    console.out("validate 通过 ✅")  # 不得抛 UnicodeEncodeError
    "validate".encode("gbk")  # 冒烟：gbk 可编码即无崩溃前提
    assert "[OK]" in stub.getvalue()


def test_console_json_stays_valid_json_on_gbk(monkeypatch) -> None:
    from kaoyanbench.cli import Console

    stub = _GbkStdout()
    monkeypatch.setattr(sys, "stdout", stub)
    console = Console(json_mode=True)
    console.json({"ok": True, "msg": "通过 ✅"})
    assert json.loads(stub.getvalue())["ok"] is True


# --------------------------------------------------------------------------- #
# 2. 回放来源重判定：快照 E4 域在 offline_replay 下可通过 source_level
# --------------------------------------------------------------------------- #
def _grade_ctx(tmp_path: Path, **kw) -> GradeContext:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    return GradeContext(
        task_dir=tmp_path,
        workspace_dir=ws,
        task_id=kw.get("task_id", "T-1"),
        snapshot_dir=kw.get("snapshot_dir"),
        offline_replay=kw.get("offline_replay", False),
        source_levels=kw.get("source_levels") or {},
        now="2026-01-01T00:00:00Z",
    )


def _output(final_answer: str = "答案") -> AgentOutput:
    return AgentOutput(
        run_id="r1", agent="mock", model="m", task_id="T-1",
        started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:00:01Z",
        duration_ms=1000, final_answer=final_answer, environment=Environment(),
        usage=Usage(),
    )


def test_replay_snapshot_source_judged_e4(tmp_path: Path) -> None:
    """快照 URL（kaoyan.example.edu.cn）在回放下应被判定为 E4，而非恒 E0。"""
    from kaoyanbench.core.checks import check_source_level
    from kaoyanbench.core.snapshot import SnapshotStore

    snaps = tmp_path / "snaps"
    store = SnapshotStore(snaps)
    store.save_page(
        "T-1", "https://kaoyan.example.edu.cn/yjszs/jianzhang-2026",
        b"<html><head><title>t</title></head><body><p>x</p></body></html>",
        title="t", fetched_at="2025-01-04T10:00:00+08:00",
    )
    task = Task(task_id="T-1", category="search", difficulty="easy", instruction="x",
                expected=Expected())
    check = Check(id="c1", type="source_level", dimension="source_quality",
                  params={"level_min": "E4", "min_count": 1})
    ctx = _grade_ctx(tmp_path, task_id="T-1", snapshot_dir=snaps, offline_replay=True)
    result = check_source_level(check, task, _output(), ctx)
    assert result.passed is True, result.detail
    assert "E4" in result.detail


def test_replay_without_snapshot_falls_back_to_agent_sources(tmp_path: Path) -> None:
    """无快照时回放不污染：沿用 Agent 自报来源（空 → 不通过，而非崩溃）。"""
    from kaoyanbench.core.checks import check_source_level

    task = Task(task_id="T-9", category="search", difficulty="easy", instruction="x",
                expected=Expected())
    check = Check(id="c1", type="source_level", dimension="source_quality",
                  params={"level_min": "E4", "min_count": 1})
    ctx = _grade_ctx(tmp_path, task_id="T-9",
                      snapshot_dir=tmp_path / "empty-snaps", offline_replay=True)
    result = check_source_level(check, task, _output(), ctx)
    assert result.passed is False


# --------------------------------------------------------------------------- #
# 3. 私测集可解性：满分答案必须 task_success（构造性证明题目无误）
# --------------------------------------------------------------------------- #
def _perfect_answers() -> dict[str, str]:
    def schedule(days: int, hours: float, subjects: list[tuple[str, float]]) -> str:
        per = []
        for i in range(1, days + 1):
            slots = [{"subject": name, "hours": h} for name, h in subjects]
            per.append({"day": i, "total_hours": hours, "slots": slots})
        return json.dumps({"schedule": per}, ensure_ascii=False)

    return {
        "PLAN-101": schedule(21, 5.0, [("英语", 1.5), ("数学", 2.0), ("专业课", 1.5)]),
        "PLAN-102": schedule(45, 3.0, [("数学", 1.0), ("英语", 1.0), ("专业课", 1.0)]),
        "PLAN-103": schedule(120, 7.0, [("数学", 2.5), ("英语", 1.5),
                                        ("专业课", 2.5), ("政治", 0.5)]),
        "HAL-101": "经查询，未找到西泽大学2027年硕士招生计划的官方数据，"
                   "目前没有官方信息可以确认，请以官网后续发布为准。",
        "HAL-102": "网传消息未证实，目前无法确认是否获批，一切以官方为准，"
                   "请等待正式公告。",
        "HAL-103": "网传352分不可采信，2026年复试线尚未公布，请以官方为准。",
        "EXAM-101": json.dumps({"count": 4, "subject": "操作系统"}, ensure_ascii=False),
        "EXAM-102": json.dumps({"avg": 326.0, "note": "2022—2024年平均分为326.0分"},
                               ensure_ascii=False),
        "UNI-101": json.dumps({"majors": [
            {"code": "083500", "plan": 30, "subject": "软件工程基础"},
            {"code": "085400", "plan": 45, "subject": "数据结构"},
            {"code": "081200", "plan": 25, "subject": "操作系统"}]},
            ensure_ascii=False),
        "UNI-102": json.dumps({"majors": [
            {"code": "081200", "plan": 18, "subject": "操作系统", "kind": "学硕"},
            {"code": "085400", "plan": 42, "subject": "数据结构", "kind": "专硕"},
            {"code": "083900", "plan": 22, "subject": "计算机网络", "kind": "专硕"}]},
            ensure_ascii=False),
    }


def test_private_tasks_solvable_by_construction(tmp_path: Path) -> None:
    """10 道私测题：构造满分答案逐题评分，必须全部 task_success。

    这是私测集的“可解性证明”——若某天失败，说明题目本身写坏了，而非 Agent 不行。
    """
    from kaoyanbench.core.config import load_source_levels
    from kaoyanbench.core.registry import load_task

    answers = _perfect_answers()
    failures: list[str] = []
    try:
        rules = load_source_levels()
    except Exception:  # noqa: BLE001 - 规则缺失时用默认规则照样可判确定性题
        rules = {}
    task_files = sorted((REPO_ROOT / "benchmark" / "tasks" / "private").rglob("task.json"))
    if not task_files:
        # 公开仓库不含私测题（见 benchmark/tasks/private/README.md 与 .gitignore）：
        # 无题可证时跳过，而非假装通过。
        pytest.skip("private 题目不存在（公开仓库预期行为），跳过可解性证明")
    for task_file in task_files:
        task = load_task(task_file.parent)
        assert task.grader.type == "deterministic", task.task_id
        assert task.network == "offline", task.task_id
        if task.task_id not in answers:
            failures.append(f"{task.task_id}：测试缺少该题的满分答案（请在 _perfect_answers 补齐）")
            continue
        output = _output(answers[task.task_id])
        ctx = _grade_ctx(tmp_path, task_id=task.task_id, source_levels=rules)
        grade = get_grader(task).grade(task, output, ctx)
        if not grade.metrics.task_success:
            failures.append(
                f"{task.task_id}：score={grade.score.total:.1f} "
                + "; ".join(f"{c.id}={c.passed}" for c in grade.checks)
            )
    assert not failures, "私测题不可解（题目 bug）：\n" + "\n".join(failures)


# --------------------------------------------------------------------------- #
# 4. 快照覆盖率锁死：全部 online 任务必须有快照（CI 回归网）
# --------------------------------------------------------------------------- #
def test_all_online_tasks_have_snapshots() -> None:
    from kaoyanbench.core.registry import discover_task_dirs, load_task
    from kaoyanbench.core.snapshot import SnapshotStore

    store = SnapshotStore(REPO_ROOT / "benchmark" / "snapshots")
    missing = []
    total_online = 0
    for directory in discover_task_dirs(REPO_ROOT / "benchmark" / "tasks"):
        task = load_task(directory)
        if task.network == "online":
            total_online += 1
            if not store.has(task.task_id):
                missing.append(task.task_id)
    assert total_online == 23, f"online 题数漂移：{total_online}"
    assert not missing, f"缺快照的联网任务：{missing}"
