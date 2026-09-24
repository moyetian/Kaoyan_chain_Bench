"""B-03：任务加载 / 过滤 / 校验。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_task, write_task

from kaoyanbench.core.errors import TaskValidationError
from kaoyanbench.core.registry import (
    discover_task_dirs,
    filter_tasks,
    get_task,
    index_tasks,
    load_task,
    validate_tasks,
)


def _write_basic(project: Path, split: str = "public") -> Path:
    task = make_task(
        "SEARCH-001",
        category="search",
        difficulty="easy",
        tags=["smoke"],
        checks=[{"id": "c1", "type": "string_contains", "dimension": "factuality",
                 "params": {"any_of": ["招生"]}}],
    )
    return write_task(project, split, task)


def test_load_task_ok(project: Path) -> None:
    directory = _write_basic(project)
    task = load_task(directory)
    assert task.task_id == "SEARCH-001"
    assert task.category == "search"


def test_discover_task_dirs(project: Path) -> None:
    _write_basic(project)
    dirs = discover_task_dirs(project / "benchmark" / "tasks")
    assert len(dirs) == 1
    assert dirs[0].name == "SEARCH-001"


def test_load_task_missing_field_reports_path(project: Path) -> None:
    directory = project / "benchmark" / "tasks" / "public" / "search" / "SEARCH-002"
    directory.mkdir(parents=True)
    (directory / "task.json").write_text(json.dumps({"task_id": "SEARCH-002"}), encoding="utf-8")
    with pytest.raises(TaskValidationError) as exc:
        load_task(directory)
    msg = str(exc.value)
    assert "SEARCH-002" in msg  # 含文件路径
    assert "difficulty" in msg or "instruction" in msg  # 含字段路径


def test_filter_by_category(project: Path) -> None:
    _write_basic(project)
    write_task(project, "public", make_task("UNI-001", category="university"))
    tasks = [load_task(d) for d in discover_task_dirs(project / "benchmark" / "tasks")]
    out = filter_tasks(tasks, categories=["search"])
    assert [t.task_id for t in out] == ["SEARCH-001"]


def test_filter_by_tag(project: Path) -> None:
    _write_basic(project)
    tasks = [load_task(d) for d in discover_task_dirs(project / "benchmark" / "tasks")]
    assert len(filter_tasks(tasks, tags=["smoke"])) == 1
    assert len(filter_tasks(tasks, tags=["nope"])) == 0


def test_index_and_get_task(project: Path) -> None:
    _write_basic(project)
    tasks = [load_task(d) for d in discover_task_dirs(project / "benchmark" / "tasks")]
    index = index_tasks(tasks)
    assert "SEARCH-001" in index
    assert get_task(tasks, "SEARCH-001").category == "search"


def test_get_task_missing_raises(project: Path) -> None:
    with pytest.raises(KeyError):
        get_task([], "NOPE-001")


def test_validate_ok_no_errors(project: Path) -> None:
    _write_basic(project)
    tasks = [load_task(d) for d in discover_task_dirs(project / "benchmark" / "tasks")]
    issues = validate_tasks(tasks)
    errors = [i for i in issues if i.level == "error"]
    assert errors == []


def test_validate_online_ground_truth_is_error(project: Path) -> None:
    task = make_task(
        "SEARCH-003",
        category="search",
        network="online",
        ground_truth={"x": 1},
        checks=[{"id": "c1", "type": "string_contains", "dimension": "factuality",
                 "params": {"any_of": ["a"]}}],
    )
    write_task(project, "public", task)
    tasks = [load_task(d) for d in discover_task_dirs(project / "benchmark" / "tasks")]
    issues = validate_tasks(tasks)
    errors = [i for i in issues if i.level == "error"]
    assert any("ground_truth" in i.message for i in errors)


def test_validate_duplicate_task_id(project: Path) -> None:
    from kaoyanbench.core.registry import load_task as _lt

    d1 = _write_basic(project)
    tasks = [_lt(d1), _lt(d1)]
    issues = validate_tasks(tasks)
    errors = [i for i in issues if i.level == "error"]
    assert any("重复" in i.message for i in errors)


def test_validate_bad_enum_is_error(project: Path) -> None:
    task = make_task("SEARCH-004", category="search", difficulty="impossible")
    write_task(project, "public", task)
    tasks = [load_task(d) for d in discover_task_dirs(project / "benchmark" / "tasks")]
    issues = validate_tasks(tasks)
    assert any(i.level == "error" and "difficulty" in i.message for i in issues)


def test_validate_ground_truth_offline_needs_references(project: Path) -> None:
    task = make_task(
        "EXAM-001",
        category="exam",
        network="offline",
        ground_truth={"answer": 42},
        checks=[{"id": "c1", "type": "numeric", "dimension": "factuality",
                 "params": {"expected": 42}}],
    )
    write_task(project, "public", task)
    tasks = [load_task(d) for d in discover_task_dirs(project / "benchmark" / "tasks")]
    issues = validate_tasks(tasks)
    # 缺少 references/ 目录 → error
    assert any(i.level == "error" and "references" in (i.field or "") for i in issues)


# --------------------------------------------------------------------------- #
# BUG-1 回归：load_suite_tasks 必须恒返回 list[Task]，绝不返回 Suite
# --------------------------------------------------------------------------- #
def test_load_suite_tasks_returns_list_even_for_unresolved_manifest(project: Path) -> None:
    """BUG-1：cli 用 load_suite_manifest 构造的 suite 没有 resolved_tasks，
    旧实现会 fallback 返回 Suite（→ TypeError: 'Suite' object is not iterable）。
    修复后必须重新解析并返回 list[Task]。"""
    from kaoyanbench.core.config import load_suite_manifest
    from kaoyanbench.core.models import Suite, Task
    from kaoyanbench.core.registry import load_suite_tasks

    write_task(project, "public", make_task("SEARCH-001", category="search"))
    write_task(project, "public", make_task("SEARCH-002", category="search"))
    suite_dir = project / "benchmark" / "suites"
    (suite_dir / "mini.yaml").write_text(
        "id: mini\nsplit: public\nversion: \"1.0\"\ntask_ids: all\nfilters:\n  categories: [search]\n",
        encoding="utf-8",
    )
    tasks_root = project / "benchmark" / "tasks"

    # load_suite_manifest 不解析任务 → 无 resolved_tasks（正是旧 bug 的触发条件）
    manifest = load_suite_manifest(suite_dir / "mini.yaml")
    assert not hasattr(manifest, "resolved_tasks")

    result = load_suite_tasks(manifest, tasks_root)
    assert isinstance(result, list)
    assert not isinstance(result, Suite)
    assert all(isinstance(t, Task) for t in result)
    assert {t.task_id for t in result} == {"SEARCH-001", "SEARCH-002"}


def test_load_suite_tasks_raises_suite_validation_error_when_unresolvable(project: Path) -> None:
    """无法解析时应抛 SuiteValidationError（带 id/路径），而不是 TypeError。"""
    from kaoyanbench.core.errors import SuiteValidationError
    from kaoyanbench.core.models import Suite
    from kaoyanbench.core.registry import load_suite_tasks

    # 直接构造一个「无 resolved_tasks 且 path 指向不存在文件」的 suite
    bogus = Suite(id="ghost", path=str(project / "benchmark" / "suites" / "ghost.yaml"))
    with pytest.raises(SuiteValidationError) as exc:
        load_suite_tasks(bogus, project / "benchmark" / "tasks")
    msg = str(exc.value)
    assert "ghost" in msg
