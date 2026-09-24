"""任务注册表：目录扫描 / 加载 / 校验 / 过滤（方案 5.1，B-03）。

设计要点：
- ``load_task`` 的错误必须**带文件路径 + 字段路径 + 期望类型**，方便出题人定位。
- ``validate_tasks`` **不抛异常**，返回全部 issue。
- 扫描顺序显式排序（``task_id`` 升序），不依赖文件系统顺序 —— 可复现性要求。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..utils.hashing import sha256_bytes
from ..utils.mini_yaml import load as load_yaml_file
from .errors import ConfigError, SuiteValidationError, TaskValidationError
from .models import (
    CATEGORIES,
    CHECK_DEFAULT_DIMENSION,
    CHECK_TYPES,
    DIFFICULTIES,
    DIMENSIONS,
    GRADER_TYPES,
    NETWORKS,
    SPLITS,
    AnswerFormat,
    Expected,
    GraderSpec,
    Suite,
    Task,
    ValidationIssue,
)

__all__ = [
    "TASK_FILENAME",
    "EXPECTED_FILENAME",
    "MANIFEST_FILENAME",
    "load_task",
    "load_task_dir",
    "load_suite",
    "validate_tasks",
    "get_task",
    "index_tasks",
    "filter_tasks",
    "discover_task_dirs",
    "load_suite_tasks",
    "validate_fixtures",
    "load_tasks_tolerant",
]

TASK_FILENAME = "task.json"
EXPECTED_FILENAME = "expected.json"
MANIFEST_FILENAME = "manifest.json"

_WEIGHT_TOL = 1e-6


# --------------------------------------------------------------------------- #
# 读取工具
# --------------------------------------------------------------------------- #
def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise TaskValidationError("文件不存在", file_path=str(path))
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise TaskValidationError(
            f"文件不是合法 UTF-8（{exc.reason}）", file_path=str(path)
        ) from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TaskValidationError(
            f"JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）",
            file_path=str(path),
        ) from None


def _require(value: Any, key: str, expected: str, path: Path) -> Any:
    if value is None:
        raise TaskValidationError(
            "缺少必填字段", file_path=str(path), field_path=key, expected=expected
        )
    return value


def _check_type(value: Any, key: str, types: Any, path: Path, expected: str) -> Any:
    if not isinstance(value, types):
        raise TaskValidationError(
            f"字段类型不正确（实际 {type(value).__name__}）",
            file_path=str(path),
            field_path=key,
            expected=expected,
        )
    return value


# --------------------------------------------------------------------------- #
# 单任务加载
# --------------------------------------------------------------------------- #
def load_task(task_dir: str | Path) -> Task:
    """加载单个任务目录（方案 5.1）。

    读 ``<task_dir>/task.json``；若存在 ``expected.json`` 则以**外置文件优先**
    （外置便于 review，方案 3.4 推荐），二者都有时外置文件整体覆盖 ``expected``。
    """
    directory = Path(task_dir)
    if not directory.is_dir():
        raise TaskValidationError("任务目录不存在", file_path=str(directory))
    task_file = directory / TASK_FILENAME
    raw = _read_json(task_file)
    if not isinstance(raw, Mapping):
        raise TaskValidationError(
            "task.json 顶层必须是对象", file_path=str(task_file), expected="object"
        )
    data = dict(raw)

    expected_file = directory / EXPECTED_FILENAME
    if expected_file.is_file():
        external = _read_json(expected_file)
        if not isinstance(external, Mapping):
            raise TaskValidationError(
                "expected.json 顶层必须是对象", file_path=str(expected_file), expected="object"
            )
        merged = dict(data.get("expected") or {})
        merged.update(dict(external))
        data["expected"] = merged

    # 目录推导 split / category，并与声明比对
    derived_category = _derived_category(directory)
    derived_split = _derived_split(directory)

    network = _require(data.get("network", "offline"), "network", "str", task_file)
    _check_type(network, "network", str, task_file, "str")
    if network not in NETWORKS:
        raise TaskValidationError(
            f"network 取值非法：{network}",
            file_path=str(task_file),
            field_path="network",
            expected=" | ".join(NETWORKS),
        )

    expected_block = data.get("expected") or {}
    grader_block = data.get("grader") or {}
    if not isinstance(expected_block, Mapping):
        raise TaskValidationError(
            "expected 必须是对象", file_path=str(task_file), field_path="expected", expected="object"
        )
    if not isinstance(grader_block, Mapping):
        raise TaskValidationError(
            "grader 必须是对象", file_path=str(task_file), field_path="grader", expected="object"
        )

    task = Task(
        task_id=str(_require(data.get("task_id"), "task_id", "str", task_file)),
        category=str(
            data.get("category") or derived_category or ""
        ),
        difficulty=str(_require(data.get("difficulty"), "difficulty", "str", task_file)),
        instruction=str(_require(data.get("instruction"), "instruction", "str", task_file)),
        network=str(network),
        time_limit=int(data.get("time_limit", 900) or 900),
        tool_limit=int(data.get("tool_limit", 60) or 60),
        expected=Expected.from_dict(expected_block),
        grader=GraderSpec.from_dict(grader_block),
        split=str(data.get("split") or derived_split or "public"),
        tags=[str(t) for t in (data.get("tags") or [])],
        references_dir=str(data.get("references_dir", "references") or "references"),
        requires_snapshot=bool(data.get("requires_snapshot", False)),
        answer_format=AnswerFormat.from_dict(data.get("answer_format")),
        version=str(data.get("version", "1.0") or "1.0"),
        created_at=data.get("created_at"),
        title=data.get("title"),
        task_dir=str(directory),
    )

    if derived_category and task.category != derived_category:
        raise TaskValidationError(
            f"category 与目录不一致（目录={derived_category}，声明={task.category}）",
            file_path=str(task_file),
            field_path="category",
            expected=derived_category,
        )
    if derived_split and task.split != derived_split:
        raise TaskValidationError(
            f"split 与目录不一致（目录={derived_split}，声明={task.split}）",
            file_path=str(task_file),
            field_path="split",
            expected=derived_split,
        )
    return task


def _derived_category(directory: Path) -> str | None:
    parent = directory.parent.name
    return parent if parent in CATEGORIES else None


def _derived_split(directory: Path) -> str | None:
    for part in reversed(directory.parts[:-1]):
        if part in SPLITS:
            return part
    return None


# --------------------------------------------------------------------------- #
# 目录扫描
# --------------------------------------------------------------------------- #
def discover_task_dirs(root: str | Path, split: str | None = None) -> list[Path]:
    """返回任务目录（按路径升序）。

    支持两种布局（兼容方案 3.3 的 ``<split>/<category>/<task_id>``）：
    - 指定 ``split``：``root/<split>/<category>/<task_id>/task.json``
    - 不指定 ``split``：同时扫描 ``root/<split>/<category>/<task_id>`` 与
      ``root/<category>/<task_id>``，覆盖 public/private 全部任务。
    """
    base = Path(root)
    if not base.is_dir():
        return []
    if split:
        search_roots = [base / split]
        depth = 2
    else:
        search_roots = [base / s for s in SPLITS] + [base]
        depth = 2
    pattern = "/".join(["*"] * depth) + f"/{TASK_FILENAME}"
    found: list[Path] = []
    for search_root in search_roots:
        if not search_root.is_dir():
            continue
        found.extend(p.parent for p in search_root.glob(pattern) if p.is_file())
    return sorted(set(found), key=lambda p: str(p))


def load_task_dir(
    root: str | Path,
    split: str = "public",
    categories: list[str] | None = None,
    difficulties: list[str] | None = None,
    tags: list[str] | None = None,
) -> list[Task]:
    """递归扫描 ``root/<split>/<category>/<task_id>/task.json``，按 ``task_id`` 升序返回。"""
    tasks = [load_task(d) for d in discover_task_dirs(root, split)]
    if categories:
        wanted = {str(c) for c in categories}
        tasks = [t for t in tasks if t.category in wanted]
    if difficulties:
        wanted_d = {str(d) for d in difficulties}
        tasks = [t for t in tasks if t.difficulty in wanted_d]
    if tags:
        wanted_t = {str(x) for x in tags}
        tasks = [t for t in tasks if wanted_t & set(t.tags)]
    tasks.sort(key=lambda t: t.task_id)
    return tasks


def filter_tasks(
    tasks: Sequence[Task],
    *,
    split: str | None = None,
    categories: Sequence[str] | None = None,
    difficulties: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    task_ids: Sequence[str] | None = None,
) -> list[Task]:
    """内存过滤（供 ``list`` / ``run`` 复用），保持输入顺序。"""
    result = list(tasks)
    if split:
        result = [t for t in result if t.split == split]
    if categories:
        wanted = {str(c) for c in categories}
        result = [t for t in result if t.category in wanted]
    if difficulties:
        wanted_d = {str(d) for d in difficulties}
        result = [t for t in result if t.difficulty in wanted_d]
    if tags:
        wanted_t = {str(x) for x in tags}
        result = [t for t in result if wanted_t & set(t.tags)]
    if task_ids:
        wanted_ids = {str(x) for x in task_ids}
        result = [t for t in result if t.task_id in wanted_ids]
    return result


def index_tasks(tasks: Iterable[Task]) -> dict[str, Task]:
    return {t.task_id: t for t in tasks}


def get_task(tasks: Sequence[Task], task_id: str) -> Task:
    """O(1) 索引查找；不存在抛 ``KeyError(f"task not found: {task_id}")``。"""
    index = index_tasks(tasks) if not isinstance(tasks, Mapping) else tasks  # type: ignore[arg-type]
    try:
        return index[task_id]  # type: ignore[index]
    except KeyError:
        raise KeyError(f"task not found: {task_id}") from None


# --------------------------------------------------------------------------- #
# Suite manifest
# --------------------------------------------------------------------------- #
def load_suite(suite_path: str | Path, tasks_root: str | Path) -> Suite:
    """加载 suite manifest 并解析出实际任务列表。

    - ``task_ids: "all"`` → 用 ``filters`` 在全量任务里筛。
    - 显式列表 → 校验每个 id 都存在，否则 ``SuiteValidationError``。
    - ``tasks_root`` 为 ``benchmark/tasks``；会按 ``split`` 子目录扫描。
    """
    path = Path(suite_path)
    if path.is_dir():
        path = path / "suite.yaml"
    if not path.is_file():
        raise SuiteValidationError(f"suite manifest 不存在：{path}")

    try:
        raw = load_yaml_file(path)
    except ConfigError as exc:
        raise SuiteValidationError(f"suite manifest 解析失败：{path.name} —— {exc.message}") from None
    if not isinstance(raw, Mapping):
        raise SuiteValidationError(f"suite manifest 顶层必须是映射：{path.name}")

    suite_id = str(raw.get("id") or path.stem)
    split = str(raw.get("split", "public") or "public")
    if split not in SPLITS:
        raise SuiteValidationError(
            f"suite '{suite_id}' 的 split 非法：{split}（合法值：{', '.join(SPLITS)}）"
        )

    defaults = dict(raw.get("defaults") or {})
    if "runs" in defaults and int(defaults["runs"]) < 1:
        raise SuiteValidationError(f"suite '{suite_id}' 的 defaults.runs 必须 >= 1")
    if "time_limit" in defaults and int(defaults["time_limit"]) <= 0:
        raise SuiteValidationError(f"suite '{suite_id}' 的 defaults.time_limit 必须为正整数")
    if "tool_limit" in defaults and int(defaults["tool_limit"]) <= 0:
        raise SuiteValidationError(f"suite '{suite_id}' 的 defaults.tool_limit 必须为正整数")

    spec = Suite(
        id=suite_id,
        split=split,
        version=str(raw.get("version", "1.0") or "1.0"),
        description=str(raw.get("description", "") or ""),
        task_ids=raw.get("task_ids", "all") if raw.get("task_ids") is not None else "all",
        filters=dict(raw.get("filters") or {}),
        defaults=defaults,
        path=str(path),
    )
    if isinstance(spec.task_ids, str) and spec.task_ids != "all":
        spec.task_ids = [t.strip() for t in spec.task_ids.split(",") if t.strip()]

    # 解析任务
    all_tasks = load_task_dir(tasks_root, split=split)
    index = index_tasks(all_tasks)
    if spec.task_ids == "all":
        filters = spec.filters
        resolved = filter_tasks(
            all_tasks,
            split=split,
            categories=filters.get("categories"),
            difficulties=filters.get("difficulties"),
            tags=filters.get("tags"),
        )
        spec.resolved_tasks = resolved  # type: ignore[attr-defined]
        return spec

    missing = [tid for tid in spec.task_ids if tid not in index]
    if missing:
        raise SuiteValidationError(
            f"suite '{suite_id}' 引用了不存在的 task_id：{', '.join(str(m) for m in missing)}"
        )
    resolved = [index[tid] for tid in spec.task_ids]
    resolved.sort(key=lambda t: t.task_id)
    spec.resolved_tasks = resolved  # type: ignore[attr-defined]
    return spec


def load_suite_tasks(suite: Suite, tasks_root: str | Path) -> list[Task]:
    """取 suite 解析后的任务列表（``load_suite`` 的便捷封装）。

    返回类型**恒为 ``list[Task]``**——绝不返回 ``Suite``。
    当 ``suite`` 是未经解析的 manifest（例如由
    :func:`kaoyanbench.core.config.load_suite_manifest` 构造，没有
    ``resolved_tasks`` 属性）时，用路径重新 ``load_suite`` 补齐任务解析。

    若重解析后仍拿不到 ``Task`` 列表（manifest 结构异常等），抛
    :class:`SuiteValidationError`（带 suite id 与路径），而不是让
    ``TypeError`` 冒到 CLI。
    """
    resolved = getattr(suite, "resolved_tasks", None)
    if resolved is not None:
        return list(resolved)

    suite_ref = suite.path or suite.id
    rehydrated = load_suite(suite_ref, tasks_root)
    tasks = getattr(rehydrated, "resolved_tasks", None)
    if tasks is None or isinstance(tasks, Suite):
        raise SuiteValidationError(
            f"suite '{suite.id}'（路径：{getattr(suite, 'path', None) or suite_ref}）"
            f"未能解析出任务列表：manifest 缺少可解析的 task_ids/filters"
        )
    return list(tasks)


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
#: 运行时快照库的索引文件名（与 ``core/snapshot.py`` 的 ``INDEX_FILENAME`` 一致）
SNAPSHOT_INDEX_FILENAME = "index.json"


def _snapshot_root_for(task: Task, snapshot_dir: str | Path | None) -> Path | None:
    """定位**运行时快照库**根目录（``benchmark/snapshots``）。

    优先用显式传入的 ``snapshot_dir``（``config.snapshot_dir``）；否则从任务目录
    向上找到含 ``tasks/`` 的 ``benchmark/`` 目录再推导。找不到返回 ``None``。
    """
    if snapshot_dir:
        return Path(snapshot_dir)
    if not task.task_dir:
        return None
    for parent in Path(task.task_dir).resolve().parents:
        if parent.name == "benchmark" and (parent / "tasks").is_dir():
            return parent / "snapshots"
    return None


def validate_tasks(
    tasks: Sequence[Task],
    *,
    snapshot_dir: str | Path | None = None,
) -> list[ValidationIssue]:
    """返回全部校验问题（**不抛异常**）。方案 5.1 的必检项逐条实现。"""
    issues: list[ValidationIssue] = []
    seen: dict[str, int] = {}

    # 排序保证 issue 顺序稳定（可复现）
    for task in sorted(tasks, key=lambda t: t.task_id):
        where = task.task_id or "(unknown)"
        tfile = str(Path(task.task_dir) / TASK_FILENAME) if task.task_dir else None

        def err(msg: str, field: str | None = None) -> None:
            issues.append(ValidationIssue("error", msg, where, field, tfile))

        def warn(msg: str, field: str | None = None) -> None:
            issues.append(ValidationIssue("warn", msg, where, field, tfile))

        # 1) task_id 唯一 + 格式
        if task.task_id in seen:
            err(f"task_id 重复（已在前一处出现）：{task.task_id}", "task_id")
        else:
            seen[task.task_id] = 1
        if not task.task_id:
            err("task_id 为空", "task_id")
        elif not _matches_task_id(task.task_id):
            err(
                f"task_id 格式不合规：{task.task_id}",
                "task_id",
            )
        else:
            prefix = task.task_id.split("-")[0]
            if task.category and not _category_prefix_ok(task.category, prefix):
                warn(
                    f"task_id 前缀 {prefix} 与 category={task.category} 的惯例前缀不同",
                    "task_id",
                )

        # 2) 枚举
        if task.category not in CATEGORIES:
            err(f"category 不在枚举内：{task.category}", "category")
        if task.difficulty not in DIFFICULTIES:
            err(f"difficulty 不在枚举内：{task.difficulty}", "difficulty")
        if task.network not in NETWORKS:
            err(f"network 不在枚举内：{task.network}", "network")
        if task.split not in SPLITS:
            err(f"split 不在枚举内：{task.split}", "split")

        # 3) 时间/工具预算
        if task.time_limit <= 0:
            err(f"time_limit 必须为正整数，当前为 {task.time_limit}", "time_limit")
        if task.time_limit > 3600:
            warn(f"time_limit={task.time_limit}s 超过 1 小时，建议评估必要性", "time_limit")
        if task.tool_limit <= 0:
            err(f"tool_limit 必须为正整数，当前为 {task.tool_limit}", "tool_limit")
        if task.tool_limit > 500:
            warn(f"tool_limit={task.tool_limit} 偏大（方案建议 <= 100）", "tool_limit")

        # 4) instruction 非空
        if not task.instruction.strip():
            err("instruction 为空", "instruction")

        # 5) grader 类型与权重
        if task.grader.type not in GRADER_TYPES:
            err(f"grader.type 不在枚举内：{task.grader.type}", "grader.type")
        if not 0.0 <= task.grader.pass_threshold <= 100.0:
            err(
                f"grader.pass_threshold 必须在 0~100，当前为 {task.grader.pass_threshold}",
                "grader.pass_threshold",
            )
        if task.grader.weights:
            unknown = sorted(set(task.grader.weights) - set(DIMENSIONS))
            if unknown:
                err(f"grader.weights 含未知维度：{', '.join(unknown)}", "grader.weights")
            total = sum(float(v) for v in task.grader.weights.values())
            if abs(total - 1.0) > _WEIGHT_TOL:
                err(
                    f"grader.weights 求和必须为 1.0（容差 1e-6），当前为 {total:.6f}",
                    "grader.weights",
                )
            negatives = sorted(k for k, v in task.grader.weights.items() if float(v) < 0)
            if negatives:
                err(f"grader.weights 含负权重：{', '.join(negatives)}", "grader.weights")

        # 6) ground_truth 硬规则（方案 4.2.1 ★）
        has_gt = bool(task.expected.ground_truth)
        if has_gt and task.network == "online":
            err(
                "network=online 的任务不得写 ground_truth（真实院校当年数字不可编造）；"
                "请改为 must_find + required_sources + 年份要求，并置 ground_truth=null",
                "expected.ground_truth",
            )
        if has_gt and task.network == "hybrid":
            warn(
                "network=hybrid 且带 ground_truth，请确认数字来自 fixture 而非真实院校",
                "expected.ground_truth",
            )
        if has_gt and task.network == "offline":
            refs = Path(task.task_dir or ".") / task.references_dir
            if not refs.is_dir():
                err(
                    "offline 任务带 ground_truth 但缺少 references/ 目录（数字必须有 fixture 支撑）",
                    "references_dir",
                )

        # 7) checks 的维度与类型
        check_ids: dict[str, int] = {}
        for idx, check in enumerate(task.grader.checks):
            cwhere = f"grader.checks[{idx}]"
            if check.id in check_ids:
                err(f"{cwhere}.id 重复：{check.id}", f"{cwhere}.id")
            check_ids[check.id] = 1
            if not check.id:
                err(f"{cwhere} 缺少 id", f"{cwhere}.id")
            if check.type not in CHECK_TYPES:
                err(f"{cwhere}.type 非法：{check.type}", f"{cwhere}.type")
            if check.dimension not in DIMENSIONS:
                err(f"{cwhere}.dimension 非法：{check.dimension}", f"{cwhere}.dimension")
            if check.type in CHECK_DEFAULT_DIMENSION and check.dimension != CHECK_DEFAULT_DIMENSION[check.type]:
                # 仅提示：允许显式覆盖，但 must_not_claim 必须 critical
                pass
            if float(check.weight) < 0:
                err(f"{cwhere}.weight 不能为负", f"{cwhere}.weight")
            if check.type == "must_not_claim" and not check.critical:
                warn(
                    f"{cwhere}（must_not_claim）建议 critical=true，否则命中幻觉不改判定",
                    f"{cwhere}.critical",
                )
            if check.type == "point_hit":
                pid = check.get("point_id")
                if pid and pid not in {p.id for p in task.expected.must_find}:
                    err(
                        f"{cwhere}.point_id 引用了不存在的要点：{pid}",
                        f"{cwhere}.point_id",
                    )
                if not pid and not check.get("any_of") and not check.get("all_of"):
                    err(
                        f"{cwhere} 需要 point_id 或 any_of/all_of 之一",
                        cwhere,
                    )

        # 7.1) 确定性 grader 无任何 check → 无法评分（运行时会被兜底为 FAIL）
        if not task.grader.checks and "semantic" not in task.grader.type:
            warn(
                "grader.type 为确定性但 checks 为空，运行时无法计算分数（将兜底为 FAIL）；"
                "请补充 grader.checks",
                "grader.checks",
            )

        # 8) semantic 必填 rubric
        if "semantic" in task.grader.type:
            if task.grader.rubric is None or not task.grader.rubric.criteria:
                err("grader.type 含 semantic 时 rubric.criteria 必须非空", "grader.rubric.criteria")
            elif not task.grader.rubric.prompt_version:
                err("grader.rubric.prompt_version 不能为空", "grader.rubric.prompt_version")

        # 9) answer_format 合法性
        if task.answer_format.kind not in ("text", "json", "file", "hybrid"):
            err(f"answer_format.kind 非法：{task.answer_format.kind}", "answer_format.kind")
        if task.answer_format.needs_file and not task.answer_format.expected_filename:
            err(
                "answer_format.kind 为 file/hybrid 时必须声明 filename 或 file",
                "answer_format.filename",
            )

        # 10) must_find 权重与 id
        point_ids: dict[str, int] = {}
        for pidx, point in enumerate(task.expected.must_find):
            if not point.id:
                err(f"expected.must_find[{pidx}] 缺少 id", f"expected.must_find[{pidx}].id")
            elif point.id in point_ids:
                err(f"expected.must_find id 重复：{point.id}", f"expected.must_find[{pidx}].id")
            point_ids[point.id] = 1
            if point.dimension not in DIMENSIONS:
                err(
                    f"expected.must_find[{pidx}].dimension 非法：{point.dimension}",
                    f"expected.must_find[{pidx}].dimension",
                )
            if not point.any_of and not point.all_of:
                warn(
                    f"expected.must_find[{pidx}]（{point.id}）既无 any_of 也无 all_of，无法判定",
                    f"expected.must_find[{pidx}]",
                )

        # 11) required_sources 等级
        for sidx, req in enumerate(task.expected.required_sources):
            if req.level_min not in ("E0", "E1", "E2", "E3", "E4", "E5"):
                err(
                    f"expected.required_sources[{sidx}].level_min 非法：{req.level_min}",
                    f"expected.required_sources[{sidx}].level_min",
                )
            if req.min_count < 1:
                err(
                    f"expected.required_sources[{sidx}].min_count 必须 >= 1",
                    f"expected.required_sources[{sidx}].min_count",
                )
            if req.dimension not in DIMENSIONS:
                err(
                    f"expected.required_sources[{sidx}].dimension 非法：{req.dimension}",
                    f"expected.required_sources[{sidx}].dimension",
                )

        # 12) 联网题无来源要求 + 无 must_find → 提示
        if task.network == "online" and not task.expected.must_find and not task.expected.required_sources:
            warn(
                "联网题既无 must_find 也无 required_sources，自动评分难以判定",
                "expected",
            )

        # 13) must_not_claim 命中即幻觉但没有任何 check 覆盖它
        if task.expected.must_not_claim and not any(
            c.type in ("must_not_claim", "point_hit") for c in task.grader.checks
        ):
            warn(
                "expected.must_not_claim 非空但无对应的 must_not_claim 检查项，抗幻觉能力不会被评测",
                "grader.checks",
            )

        # 13.1) anti-echo：check 的判定词原样出现在 instruction 中 → 回显指令即可得分
        #       （方案 6.5「避免记忆题」精神；QA P1-2 的能力支撑，仅 warn 不阻断）
        for leaked_idx, leaked_token in _echo_leaked_tokens(task):
            warn(
                f"grader.checks[{leaked_idx}] 的判定词「{leaked_token}」原样出现在 instruction 中，"
                "回显指令即可命中（基准可判别性弱，建议改为需计算/检索才可得）",
                f"grader.checks[{leaked_idx}]",
            )

        # 14) 任务 README 与 fixture manifest
        tdir = Path(task.task_dir) if task.task_dir else None
        if tdir is not None:
            if not (tdir / "README.md").is_file():
                warn("任务目录缺少 README.md（方案 6.5 要求写明考点与陷阱）", "README.md")
            issues.extend(_validate_references(task, err, warn))

        # 15) snapshot 存在性（P1，仅告警）—— 检查**运行时快照库**而非任务目录
        if (task.network in ("online", "hybrid") or task.requires_snapshot) and task.task_dir:
            snap_root = _snapshot_root_for(task, snapshot_dir)
            if snap_root is None or not (
                snap_root / task.task_id / SNAPSHOT_INDEX_FILENAME
            ).is_file():
                warn(
                    "联网/快照任务缺少 snapshots/<task_id>/index.json（运行时快照库），"
                    "将标记 needs_snapshot",
                    "requires_snapshot",
                )

    return issues


def _matches_task_id(task_id: str) -> bool:
    from .models import TASK_ID_RE

    return bool(TASK_ID_RE.match(task_id))


#: 从 check 中提取「判定词」的参数键（值可能是 str 或 list[str]）。
_ECHO_KEYWORD_KEYS: tuple[str, ...] = ("any_of", "all_of", "value", "values", "contains")

#: 判定词的最小长度：过短的词（如「的」「年」）易误报，不作为泄漏信号。
_ECHO_MIN_LEN = 2


def _check_keywords(check: Any) -> list[str]:
    """抽取单个 check 的字符串判定词（去空、去重、保序）。"""
    tokens: list[str] = []
    for key in _ECHO_KEYWORD_KEYS:
        raw = check.get(key) if hasattr(check, "get") else None
        if raw is None:
            continue
        items = raw if isinstance(raw, (list, tuple)) else [raw]
        for item in items:
            if isinstance(item, str):
                token = item.strip()
                if len(token) >= _ECHO_MIN_LEN and token not in tokens:
                    tokens.append(token)
    return tokens


def _echo_leaked_tokens(task: Task) -> list[tuple[int, str]]:
    """返回 ``[(check 索引, 判定词)]``：判定词**原样**出现在 ``instruction`` 中。

    仅针对「字符串匹配类」check（``string_contains`` / ``string_eq`` /
    ``set_includes`` / ``point_hit``），且忽略过短的词以减少误报。
    """
    instruction = task.instruction or ""
    if not instruction:
        return []
    hit: list[tuple[int, str]] = []
    for idx, check in enumerate(task.grader.checks):
        if check.type not in ("string_contains", "string_eq", "set_includes", "point_hit"):
            continue
        for token in _check_keywords(check):
            if token in instruction:
                hit.append((idx, token))
    return hit


_CATEGORY_PREFIX: dict[str, tuple[str, ...]] = {
    "search": ("SEARCH",),
    "university": ("UNI",),
    "policy": ("POL",),
    "exam": ("EXAM",),
    "pdf": ("PDF",),
    "planning": ("PLAN",),
    "research": ("RES",),
    "hallucination": ("HAL",),
}


def _category_prefix_ok(category: str, prefix: str) -> bool:
    allowed = _CATEGORY_PREFIX.get(category)
    if not allowed:
        return True
    return prefix in allowed


def _validate_references(
    task: Task, err: Any, warn: Any, *, emit_missing_dir: bool = True
) -> list[ValidationIssue]:
    """校验 references/manifest.json 中每个文件存在且 sha256 匹配（方案 5.1）。

    ``emit_missing_dir=False`` 时不产出「缺少 references/ 目录」的告警
    （该告警由 :func:`validate_tasks` 统一负责，避免 ``validate`` 重复输出）。
    """
    out: list[ValidationIssue] = []
    tdir = Path(task.task_dir or ".")
    refs = tdir / task.references_dir
    manifest_path = refs / MANIFEST_FILENAME
    if not refs.is_dir():
        if task.network == "offline" and emit_missing_dir:
            out.append(
                ValidationIssue(
                    "warn",
                    "离线任务缺少 references/ 目录（如该题确实无需 fixture 可忽略）",
                    task.task_id,
                    "references_dir",
                    str(refs),
                )
            )
        return out
    if not manifest_path.is_file():
        out.append(
            ValidationIssue(
                "warn",
                "references/ 存在但没有 manifest.json，无法校验 fixture hash",
                task.task_id,
                "references.manifest",
                str(manifest_path),
            )
        )
        return out
    try:
        manifest = _read_json(manifest_path)
    except TaskValidationError as exc:
        out.append(
            ValidationIssue("error", f"manifest.json 无法解析：{exc.message}", task.task_id,
                            "references.manifest", str(manifest_path))
        )
        return out
    if not isinstance(manifest, Mapping):
        out.append(
            ValidationIssue("error", "manifest.json 顶层必须是「文件名 → sha256」的对象",
                            task.task_id, "references.manifest", str(manifest_path))
        )
        return out

    files_block = manifest.get("files") if "files" in manifest else manifest
    if not isinstance(files_block, Mapping):
        out.append(
            ValidationIssue("error", "manifest.json 的 files 段必须是对象",
                            task.task_id, "references.manifest", str(manifest_path))
        )
        return out

    for name in sorted(files_block):
        entry = files_block[name]
        expected_hash = entry.get("sha256") if isinstance(entry, Mapping) else entry
        if not expected_hash:
            out.append(
                ValidationIssue(
                    "error",
                    f"manifest.json 中 {name} 缺少 sha256",
                    task.task_id,
                    f"references.{name}",
                    str(manifest_path),
                )
            )
            continue
        target = refs / name
        # 安全：manifest 中的文件名不得越出 references/（防路径穿越 ../）
        try:
            target.resolve().relative_to(refs.resolve())
        except (ValueError, OSError):
            out.append(
                ValidationIssue(
                    "error",
                    f"manifest.json 中的文件名非法（不得含路径分隔符或 ..）：{name}",
                    task.task_id,
                    f"references.{name}",
                    None,
                )
            )
            continue
        if not target.is_file():
            out.append(
                ValidationIssue(
                    "error",
                    f"fixture 文件缺失：{name}",
                    task.task_id,
                    f"references.{name}",
                    str(target),
                )
            )
            continue
        actual = sha256_bytes(target.read_bytes())
        if actual.lower() != str(expected_hash).lower():
            out.append(
                ValidationIssue(
                    "error",
                    f"fixture hash 不匹配：{name}（期望 {str(expected_hash)[:12]}…，"
                    f"实际 {actual[:12]}…）",
                    task.task_id,
                    f"references.{name}",
                    str(target),
                )
            )
    return out


def validate_fixtures(tasks: Sequence[Task]) -> list[ValidationIssue]:
    """只跑 fixture hash 校验（供 ``validate --fixtures-only`` 使用）。"""
    out: list[ValidationIssue] = []
    for task in sorted(tasks, key=lambda t: t.task_id):

        def err(msg: str, field: str | None = None) -> None:
            out.append(ValidationIssue("error", msg, task.task_id, field))

        def warn(msg: str, field: str | None = None) -> None:
            out.append(ValidationIssue("warn", msg, task.task_id, field))

        out.extend(_validate_references(task, err, warn, emit_missing_dir=False))
    return out


def _dedupe_issues(issues: Sequence[ValidationIssue]) -> list[ValidationIssue]:
    """按 ``(level, task_id, field, message)`` 去重，保持首次出现顺序。

    同一校验错误可能被多条扫描路径各收集一次（例如 ``validate_tasks`` 与
    ``validate_fixtures``/``_validate_references``），去重避免 CI 日志噪音与
    ``error`` 计数虚高（QA P2-2）。
    """
    seen: set[tuple[Any, ...]] = set()
    out: list[ValidationIssue] = []
    for issue in issues:
        key = (issue.level, issue.task_id, issue.field, issue.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(issue)
    return out


def load_tasks_tolerant(
    directories: Sequence[str | Path],
) -> tuple[list[Task], list[ValidationIssue]]:
    """逐个加载任务目录；**单个加载失败不中断扫描**（QA P1-1）。

    返回 ``(成功加载的 tasks, 失败产生的 issues)``。每个失败被包装成
    :class:`ValidationIssue`（``level="error"``，含 ``file`` 路径），使
    ``validate`` 能一次性汇总报出**全部**任务的问题，而不是遇到第一个坏题就退出。
    """
    tasks: list[Task] = []
    issues: list[ValidationIssue] = []
    for directory in directories:
        dir_path = Path(directory)
        task_file = dir_path / TASK_FILENAME
        try:
            tasks.append(load_task(dir_path))
        except TaskValidationError as exc:
            issues.append(
                ValidationIssue(
                    "error",
                    exc.message,
                    task_id=dir_path.name,
                    field=getattr(exc, "field_path", None),
                    file=getattr(exc, "file_path", None) or str(task_file),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 兜底：坏题不得阻断全量扫描
            issues.append(
                ValidationIssue(
                    "error",
                    f"任务加载失败（{type(exc).__name__}）：{exc}",
                    task_id=dir_path.name,
                    field=None,
                    file=str(task_file),
                )
            )
    return tasks, issues
