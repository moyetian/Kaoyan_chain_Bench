"""KaoyanBench 命令行入口（方案 5.10，B-22 / B-23）。

全局约定
--------
- ``--json`` 时 **stdout 只输出合法 JSON**，所有日志/进度走 stderr。
- 无 ``--json`` 时输出人类可读的纯文本表格（**不用任何第三方表格库**）。
- 退出码：成功 0 / 回归门禁失败 1（``--strict-warn`` 时 warn 也算 1）/ 运行错误 2。

子命令与 5.10 契约一一对应。所有涉及时间/随机的地方都从外部注入，
保证评测路径可复现。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import __version__
from .core.config import (
    Config,
    find_project_root,
    list_agent_names,
    load_agent_spec,
    load_config,
    load_semantic_config,
    load_source_levels,
)
from .core.engine import (
    RunSpec,
    default_run_id_factory,
    grade_existing_run,
    run_suite,
    summary_line,
)
from .core.errors import KaoyanBenchError, StoreError, SuiteValidationError
from .core.graders.semantic import build_semantic_client
from .core.models import (
    CATEGORIES,
    CATEGORY_LABELS,
    DIFFICULTIES,
    DIFFICULTY_LABELS,
    Suite,
    Task,
    ValidationIssue,
    parse_gate_config,
)
from .core.registry import (
    load_suite,
    load_suite_tasks,
    load_task,
    validate_fixtures,
    validate_tasks,
)
from .core.regression import (
    build_regression_report,
    exit_code_for,
    summarize_verdict,
)
from .core.reporter import FORMAT_ALIASES, build_report
from .core.snapshot import SnapshotStore, fetch_url
from .core.store import ResultStore
from .utils.timex import utcnow_iso

__all__ = ["main", "build_parser"]

PROG = "kaoyanbench"
EXIT_OK = 0
EXIT_GATE_FAIL = 1
EXIT_ERROR = 2

# 单任务重复次数的上界：防止 `--runs 999999` 之类超大值长时间占用机器（QA P3-1）。
# 50 题 × 100 次 = 5000 次运行已远超回归门禁所需。
MAX_RUNS_PER_TASK = 100


# --------------------------------------------------------------------------- #
# 输出小工具（全部纯标准库；--json 分支禁止任何日志混入 stdout）
# --------------------------------------------------------------------------- #
#: 终端不支持的符号的 ASCII 回退（Windows gbk/cp936 控制台无法编码 Emoji，
#: 裸 print 会抛 UnicodeEncodeError 导致整轮崩溃；P2 修复）。
_CONSOLE_FALLBACKS: tuple[tuple[str, str], ...] = (
    ("✅", "[OK]"),
    ("❌", "[FAIL]"),
    ("⚠", "[!]"),
    ("⚠️", "[!]"),
    ("→", "->"),
    ("×", "x"),
    ("—", "-"),
)


def _safe_console_text(text: str) -> str:
    """把文本降级为当前终端可编码的形式（中文保留，仅替换不可编码符号）。"""
    if not isinstance(text, str):
        text = str(text)
    try:
        text.encode(sys.stdout.encoding or "utf-8")
        return text
    except (UnicodeEncodeError, LookupError):
        pass
    for raw, fallback in _CONSOLE_FALLBACKS:
        text = text.replace(raw, fallback)
    try:
        text.encode(sys.stdout.encoding or "utf-8")
        return text
    except (UnicodeEncodeError, LookupError):
        return text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8", errors="replace"
        )


class Console:
    """极简文本输出器：``--json`` 模式下 stdout 保持纯净。"""

    def __init__(self, json_mode: bool, quiet: bool = False, verbose: bool = False) -> None:
        self.json_mode = json_mode
        self.quiet = quiet
        self.verbose = verbose

    # 数据输出（stdout）
    def out(self, text: str = "") -> None:
        if not self.json_mode:
            print(_safe_console_text(text))

    def json(self, payload: Any) -> None:
        print(_safe_console_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)))

    # 日志（stderr）
    def info(self, text: str) -> None:
        if not self.quiet:
            print(_safe_console_text(text), file=sys.stderr)

    def debug(self, text: str) -> None:
        if self.verbose and not self.quiet:
            print(_safe_console_text(text), file=sys.stderr)

    def warn(self, text: str) -> None:
        if not self.quiet:
            print(_safe_console_text(f"[warn] {text}"), file=sys.stderr)

    def error(self, text: str) -> None:
        print(_safe_console_text(f"[error] {text}"), file=sys.stderr)


def _table(rows: Sequence[Sequence[Any]], headers: Sequence[str]) -> str:
    """纯文本对齐表格（不使用第三方库）。"""
    cells = [[("" if c is None else str(c)) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in cells:
        lines.append(
            "  ".join(
                (row[i] if i < len(row) else "").ljust(widths[i]) for i in range(len(headers))
            ).rstrip()
        )
    return "\n".join(lines)


def _fmt_float(value: Any, digits: int = 4) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_signed(value: Any, digits: int = 1) -> str:
    """带符号格式化（用于阈值，如 ``-5.0pp``）。"""
    if value is None:
        return "-"
    try:
        return f"{float(value):+.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------------- #
# 路径 / 配置解析
# --------------------------------------------------------------------------- #
def _resolve_root(args: argparse.Namespace) -> Path:
    if getattr(args, "root", None):
        return Path(args.root).resolve()
    env_root = os.environ.get("KAOYANBENCH_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return find_project_root(Path.cwd())


def _load_config(args: argparse.Namespace) -> Config:
    root = _resolve_root(args)
    overrides: dict[str, Any] = {}
    if getattr(args, "grader_type", None):
        overrides.setdefault("evaluation", {})["grader_type"] = args.grader_type
    if getattr(args, "offline_replay", None):
        overrides.setdefault("evaluation", {})["offline_replay"] = True
    if getattr(args, "time_limit", None):
        overrides.setdefault("runtime", {})["timeout"] = int(args.time_limit)
    if getattr(args, "tool_limit", None):
        overrides.setdefault("runtime", {})["max_tool_calls"] = int(args.tool_limit)
    if getattr(args, "keep_workspace", None):
        overrides.setdefault("runtime", {})["keep_workspace"] = True
    if getattr(args, "concurrency", None):
        overrides.setdefault("runtime", {})["concurrency"] = int(args.concurrency)
    if getattr(args, "runs", None):
        overrides.setdefault("evaluation", {})["runs"] = int(args.runs)
    if getattr(args, "strict_warn", None):
        overrides.setdefault("evaluation", {})["strict_warn"] = True
    return load_config(
        Path(args.config) if getattr(args, "config", None) else None,
        root=root,
        overrides=overrides or None,
    )


def _resolve_suite_path(config: Config, suite_id: str) -> Path:
    candidate = config.suites_root / f"{suite_id}.yaml"
    if candidate.is_file():
        return candidate
    alt = config.suites_root / f"{suite_id}.yml"
    if alt.is_file():
        return alt
    # 也允许传相对/绝对路径
    given = Path(suite_id)
    if given.is_file():
        return given
    from .core.errors import ConfigError

    raise ConfigError(
        f"找不到 suite '{suite_id}'（已尝试：{candidate}）。"
        f"请在 {config.suites_root} 下创建 <id>.yaml"
    )


def _load_suite(config: Config, suite_id: str) -> tuple[Suite, list[Task]]:
    """加载 suite 并解析任务。

    直接走 :func:`kaoyanbench.core.registry.load_suite`：它一步完成
    manifest 解析 **与** 任务解析（设置 ``resolved_tasks``），因此
    ``load_suite_tasks`` 不需要再走 fallback、也不会误返回 ``Suite``。
    """
    path = _resolve_suite_path(config, suite_id)
    suite = load_suite(path, config.tasks_root)
    tasks = load_suite_tasks(suite, config.tasks_root)
    if not isinstance(tasks, list) or any(not isinstance(t, Task) for t in tasks):
        raise SuiteValidationError(
            f"suite '{suite_id}'（路径：{path}）解析出的任务列表类型非法"
        )
    return suite, tasks


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #
def cmd_list(args: argparse.Namespace, console: Console) -> int:
    config = _load_config(args)
    suite_id = args.suite or config.benchmark.default_suite

    tasks: list[Task]
    if args.task_ids_file:
        tasks = _tasks_from_file(config, args.task_ids_file)
    else:
        try:
            _suite, tasks = _load_suite(config, suite_id)
        except KaoyanBenchError:
            # suite 尚未创建时，退化为扫描全部任务
            from .core.registry import discover_task_dirs

            tasks = [load_task(d) for d in discover_task_dirs(config.tasks_root)]
            suite_id = "(all)"
        except Exception as exc:  # noqa: BLE001 - 兜底：不让裸异常冒到 CLI 崩栈
            console.error(
                f"加载 suite '{suite_id}' 失败（{type(exc).__name__}）：{exc}；"
                "已退化为扫描全部任务"
            )
            from .core.registry import discover_task_dirs

            tasks = [load_task(d) for d in discover_task_dirs(config.tasks_root)]
            suite_id = "(all)"

    # 防御：即便上游返回了非 list[Task]，也在此拦下并给出可读错误
    if not isinstance(tasks, list) or any(not isinstance(t, Task) for t in tasks):
        console.error(
            f"suite '{suite_id}' 解析出的任务列表类型非法，已中止（期望 list[Task]）"
        )
        return EXIT_ERROR

    if args.split:
        tasks = [t for t in tasks if t.split == args.split]
    if args.category:
        wanted = _split_csv(args.category)
        tasks = [t for t in tasks if t.category in wanted]
    if args.difficulty:
        wanted = _split_csv(args.difficulty)
        tasks = [t for t in tasks if t.difficulty in wanted]
    if args.tag:
        wanted = set(_split_csv(args.tag))
        tasks = [t for t in tasks if wanted & set(t.tags)]

    snap_store = SnapshotStore(config.snapshot_dir)
    rows: list[list[Any]] = []
    missing_snapshot: list[str] = []
    for t in sorted(tasks, key=lambda x: x.task_id):
        has_snap = snap_store.has(t.task_id)
        needs = t.network in ("online", "hybrid") or t.requires_snapshot
        if needs and not has_snap:
            missing_snapshot.append(t.task_id)
        rows.append(
            [
                t.task_id,
                t.category,
                t.difficulty,
                t.network,
                t.grader.type,
                t.split,
                ",".join(t.tags) or "-",
                "yes" if has_snap else ("need" if needs else "-"),
            ]
        )

    if console.json_mode:
        console.json(
            {
                "suite": suite_id,
                "task_count": len(rows),
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "category": t.category,
                        "difficulty": t.difficulty,
                        "network": t.network,
                        "grader_type": t.grader.type,
                        "split": t.split,
                        "tags": list(t.tags),
                        "title": t.title,
                        "has_snapshot": snap_store.has(t.task_id),
                        "needs_snapshot": (t.network in ("online", "hybrid") or t.requires_snapshot),
                    }
                    for t in sorted(tasks, key=lambda x: x.task_id)
                ],
                "missing_snapshot": missing_snapshot,
            }
        )
    else:
        if not rows:
            console.out("（没有匹配的任务）")
        else:
            console.out(
                _table(
                    rows,
                    ["task_id", "category", "difficulty", "network", "grader", "split", "tags", "snapshot"],
                )
            )
        if args.show_missing_snapshot and missing_snapshot:
            console.out("")
            console.out(f"缺少快照的联网/快照任务（{len(missing_snapshot)}）：")
            for tid in missing_snapshot:
                console.out(f"  - {tid}")
    return EXIT_OK


def _tasks_from_file(config: Config, path: str) -> list[Task]:
    """从 ``--tasks-file``（每行一个 task_id，``#`` 起注释）加载任务。"""
    file = Path(path)
    content = file.read_text(encoding="utf-8")
    ids = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    from .core.registry import discover_task_dirs

    index = {d.name: d for d in discover_task_dirs(config.tasks_root)}
    out: list[Task] = []
    for tid in ids:
        directory = index.get(tid)
        if directory is None:
            # 允许按 task_id 前缀匹配目录名
            matches = [d for name, d in index.items() if name.upper() == tid.upper()]
            if matches:
                directory = matches[0]
        if directory is None:
            from .core.errors import ConfigError

            raise ConfigError(f"--tasks-file 中的任务不存在：{tid}")
        out.append(load_task(directory))
    return out


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def _normalize_suite_defaults(raw: Mapping[str, Any] | None) -> dict[str, int]:
    """把 ``suite.defaults`` 规范化为 ``{runs?/time_limit?/tool_limit?: int}``。

    非法值直接丢弃（schema 校验由 ``load_suite`` 负责，此处只保证类型安全）。
    """
    out: dict[str, int] = {}
    for key in ("runs", "time_limit", "tool_limit"):
        value = (raw or {}).get(key)
        if value is None:
            continue
        try:
            out[key] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def _load_suite_defaults(config: Config, suite_id: str) -> dict[str, int]:
    """容错读取 suite 的 ``defaults`` 段（suite 不存在/不可加载时返回空 dict）。"""
    try:
        suite, _ = _load_suite(config, suite_id)
    except KaoyanBenchError:
        return {}
    return _normalize_suite_defaults(suite.defaults)


def _select_tasks(
    args: argparse.Namespace, config: Config
) -> tuple[str, str, list[Task], dict[str, Any]]:
    """返回 ``(suite_id, split, tasks, suite_defaults)``。"""
    split = args.split or ""
    suite_defaults: dict[str, Any] = {}
    if args.tasks_file:
        tasks = _tasks_from_file(config, args.tasks_file)
        suite_id = args.suite or "(tasks-file)"
        if args.suite:
            suite_defaults = _load_suite_defaults(config, args.suite)
    elif args.task:
        from .core.registry import discover_task_dirs

        wanted = set(_split_csv(args.task))
        found: dict[str, Path] = {}
        for d in discover_task_dirs(config.tasks_root):
            found[d.name] = d
        tasks = []
        for tid in sorted(wanted):
            directory = found.get(tid)
            if directory is None:
                matches = [d for name, d in found.items() if name.upper() == tid.upper()]
                if matches:
                    directory = matches[0]
            if directory is None:
                from .core.errors import ConfigError

                raise ConfigError(f"找不到任务：{tid}（已扫描 {config.tasks_root}）")
            tasks.append(load_task(directory))
        suite_id = args.suite or "(tasks)"
        if args.suite:
            suite_defaults = _load_suite_defaults(config, args.suite)
    else:
        suite_id = args.suite or config.benchmark.default_suite
        suite, tasks = _load_suite(config, suite_id)
        suite_defaults = _normalize_suite_defaults(suite.defaults)
        if not split and suite.split:
            split = suite.split

    if args.category:
        wanted = set(_split_csv(args.category))
        tasks = [t for t in tasks if t.category in wanted]
    if args.difficulty:
        wanted = set(_split_csv(args.difficulty))
        tasks = [t for t in tasks if t.difficulty in wanted]
    # 注意：``run --tag`` 是 **suite 标签**（写入结果目录名），不是任务标签过滤；
    # 任务标签过滤只在 ``list`` 中通过 _select_tasks 之外的逻辑处理。
    if split:
        tasks = [t for t in tasks if t.split == split]

    return suite_id, split or "public", tasks, suite_defaults


def cmd_run(args: argparse.Namespace, console: Console) -> int:
    config = _load_config(args)
    suite_id, split, tasks, suite_defaults = _select_tasks(args, config)
    if not tasks:
        console.error("没有匹配任何任务；请检查 --suite/--task/--split/--category 过滤条件")
        return EXIT_ERROR

    agent_spec = load_agent_spec(args.agent, config=config)
    run_id_factory = None
    if args.seed is not None:
        counter = {"n": 0}

        def run_id_factory(now: str | None = None) -> str:  # noqa: D401
            # 以 seed 派生确定性 run_id，保证 --seed 下可复现
            counter["n"] += 1
            return f"run_s{args.seed}_{counter['n']:05d}"

    store = ResultStore(config.results_dir)
    source_levels = load_source_levels(config)
    semantic_client = build_semantic_client(config.grader_model, load_semantic_config(config))

    # 优先级：CLI 显式 --runs > suite.defaults.runs > config.evaluation.runs
    runs_per_task = int(
        args.runs
        if args.runs is not None
        else (suite_defaults.get("runs") or config.evaluation.runs)
    )
    if runs_per_task < 1:
        console.error(f"--runs 必须 >= 1，当前为 {runs_per_task}")
        return EXIT_ERROR
    if runs_per_task > MAX_RUNS_PER_TASK:
        console.error(
            f"runs/task 过大（{runs_per_task}），上界为 {MAX_RUNS_PER_TASK}；"
            "请减小 --runs 或 suite.defaults.runs"
        )
        return EXIT_ERROR
    concurrency = int(args.concurrency or config.runtime.concurrency)

    spec = RunSpec(
        tasks=tasks,
        suite_id=suite_id,
        split=split,
        agent_name=args.agent,
        agent_spec=agent_spec,
        tag=args.tag or _default_tag(agent_spec),
        runs_per_task=runs_per_task,
        seed=args.seed if args.seed is not None else config.evaluation.seed,
        concurrency=concurrency,
        time_limit=args.time_limit,
        tool_limit=args.tool_limit,
        suite_time_limit=suite_defaults.get("time_limit"),
        suite_tool_limit=suite_defaults.get("tool_limit"),
        offline_replay=bool(args.offline_replay or config.evaluation.offline_replay),
        keep_workspace=bool(args.keep_workspace or config.runtime.keep_workspace),
        fail_fast=bool(args.fail_fast or config.runtime.fail_fast),
        benchmark_version=config.benchmark.version,
    )

    console.info(
        f"[run] suite={suite_id} agent={args.agent} tasks={len(tasks)} "
        f"runs/task={runs_per_task} concurrency={concurrency}"
    )

    result = run_suite(
        spec,
        config=config,
        store=store,
        semantic_client=semantic_client,
        source_levels=source_levels,
        log=lambda line: console.info(line),
        run_id_factory=run_id_factory,
    )

    store.write_suite(result.suite_result, spec.tag)
    if result.warnings:
        for w in result.warnings:
            console.warn(w)

    summary = result.suite_result.aggregates

    if console.json_mode:
        console.json(
            {
                "suite_id": suite_id,
                "agent": args.agent,
                "tag": spec.tag,
                "task_count": result.suite_result.task_count,
                "runs": len(result.suite_result.runs),
                "run_paths": [str(p) for p in result.run_paths],
                "suite_path": str(result.suite_path) if result.suite_path else None,
                "grader_mode": summary.grader_mode,
                "task_success_rate": summary.task_success_rate,
                "score_mean": summary.score_mean,
                "pass_at_1": summary.pass_at_1,
            }
        )
    else:
        rows = []
        for outcome in result.outcomes:
            grade = outcome.grade
            rows.append(
                [
                    outcome.task.task_id,
                    outcome.task.category,
                    outcome.task.difficulty,
                    grade.grader_mode,
                    _fmt_float(grade.score_total, 1),
                    "PASS" if grade.task_success else "FAIL",
                    _fmt_pct(grade.metrics.hallucination_rate),
                ]
            )
        console.out(
            _table(rows, ["task_id", "category", "difficulty", "mode", "score", "result", "halluc"])
        )
        console.out("")
        console.out(summary_line(result.suite_result))
        console.out("")
        console.out(f"results: {config.results_dir}  suite 结果: {result.suite_path}")

    # 运行错误（task_success 全失败不算错误，只有异常/管线错误才是）→ 这里统一 0
    return EXIT_OK


def _default_tag(agent_spec: Any) -> str:
    version = getattr(agent_spec, "version", None)
    return f"v{version}" if version else "v1"


# --------------------------------------------------------------------------- #
# grade
# --------------------------------------------------------------------------- #
def cmd_grade(args: argparse.Namespace, console: Console) -> int:
    config = _load_config(args)
    store = ResultStore(config.results_dir)
    run = store.read_run(args.run_id) if not Path(args.run_id).is_file() else _run_from_file(args.run_id)
    if run is None:
        console.error(f"找不到 run：{args.run_id}")
        return EXIT_ERROR

    try:
        task = _find_task(config, run.task_id)
    except KaoyanBenchError as exc:
        console.error(f"无法复评 run {run.run_id}：找不到对应任务（{exc}）")
        return EXIT_ERROR
    source_levels = load_source_levels(config)
    semantic_client = build_semantic_client(config.grader_model, load_semantic_config(config))

    grade = grade_existing_run(
        run,
        task,
        config=config,
        store=store,
        semantic_client=semantic_client,
        source_levels=source_levels,
    )

    # 真实字段是 ``errors: list[ErrorItem]``（**没有** ``error_codes``）。
    # 这里统一从 ``errors`` 提取错误码，并对字段缺失做兜底，避免裸 AttributeError 崩栈。
    error_codes = _grade_error_codes(grade)

    if console.json_mode:
        console.json(
            {
                "run_id": run.run_id,
                "task_id": run.task_id,
                "grader_type": grade.grader_type,
                "grader_mode": grade.grader_mode,
                "total_score": grade.score_total,
                "task_success": grade.task_success,
                "error_codes": error_codes,
            }
        )
    else:
        console.out(f"run_id:  {run.run_id}")
        console.out(f"task:    {run.task_id}")
        console.out(f"grader:  {grade.grader_type}（{grade.grader_mode}）")
        console.out(f"score:   {_fmt_float(grade.score_total, 1)}")
        console.out(f"结果:    {'PASS' if grade.task_success else 'FAIL'}")
        if error_codes:
            console.out(f"错误:    {', '.join(error_codes)}")
    return EXIT_OK


def _grade_error_codes(grade: Any) -> list[str]:
    """从 :class:`GradeResult` 提取错误码（去重、保持出现顺序）。

    以 ``errors: list[ErrorItem]`` 为**唯一事实源**；若对象缺少该字段或结构异常，
    退化为读取可选的 ``error_codes`` 或返回空列表——**绝不抛异常**，保证 CLI
    在字段演进时不会崩栈。
    """
    errors = getattr(grade, "errors", None)
    codes: list[str] = []
    if isinstance(errors, (list, tuple)):
        for item in errors:
            code = getattr(item, "code", None)
            if code is None and isinstance(item, Mapping):
                code = item.get("code")
            if code and str(code) not in codes:
                codes.append(str(code))
        return codes
    # 兜底：某些历史/外部实现可能只提供 error_codes
    fallback = getattr(grade, "error_codes", None)
    if isinstance(fallback, (list, tuple)):
        return [str(c) for c in fallback if c]
    return codes


def _run_from_file(path: str):
    from .core.logger import run_from_output

    return run_from_output(path)


def _find_task(config: Config, task_id: str) -> Task:
    from .core.registry import discover_task_dirs

    for d in discover_task_dirs(config.tasks_root):
        if d.name.upper() == task_id.upper():
            return load_task(d)
    from .core.errors import ConfigError

    raise ConfigError(f"找不到任务：{task_id}")


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def _report_task_meta(config: Config, task_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """从任务注册表回填 ``report.json`` 明细所需的真实元数据。

    修复：``cmd_report`` 原先未传 ``task_meta``，导致 ``tasks[].category/
    difficulty/network`` 全为 ``unknown``，前端筛选失效。这里扫描全部
    split 下的任务，取出 ``{category, difficulty, network}``。

    任务缺失/加载失败时**不抛异常**（报告仍要能产出），只是该 id 不回填。
    """
    from .core.registry import discover_task_dirs

    want = {str(t) for t in task_ids}
    if not want:
        return {}
    meta: dict[str, dict[str, Any]] = {}
    for directory in discover_task_dirs(config.tasks_root):
        try:
            task = load_task(directory)
        except KaoyanBenchError:
            continue
        if task.task_id in want:
            meta[task.task_id] = {
                "category": task.category,
                "difficulty": task.difficulty,
                "network": task.network,
            }
    return meta


def _resolve_result_ref(
    store: ResultStore,
    config: Config,
    suite_id: str,
    agent: str | None,
    tag: str | None = None,
) -> tuple[str, str]:
    """解析报告类命令的 ``(agent, tag)``。

    ``--agent`` 显式指定时以其为准；否则优先尝试配置的 ``agent.ref``（仅当确有结果），
    最后按已有结果反查。避免把「配置语义的被测对象」当成「结果检索条件」，
    导致未显式传 ``--agent`` 时读不到结果。
    """
    if agent:
        return store.resolve_suite_ref(suite_id, agent, tag)
    configured = str(config.agent.get("ref") or "") or None
    if configured:
        try:
            return store.resolve_suite_ref(suite_id, configured, tag)
        except StoreError:
            pass
    return store.resolve_suite_ref(suite_id, None, tag)


def cmd_report(args: argparse.Namespace, console: Console) -> int:
    config = _load_config(args)
    store = ResultStore(config.results_dir)
    suite_id = args.suite or config.benchmark.default_suite
    agent, tag = _resolve_result_ref(store, config, suite_id, args.agent, args.tag)

    current = store.load_suite(suite_id, agent, tag)
    baseline = None
    baseline_report = None
    if args.baseline:
        baseline = store.load_suite(suite_id, agent, args.baseline)
        from .core.regression import build_regression_report

        baseline_report = build_regression_report(
            baseline,
            current,
            gates=load_config(args.config, root=_resolve_root(args)).gates,
        )

    formats = _split_csv(args.format) if args.format else list(config.output.format)
    out_dir = Path(args.out) if args.out else config.reports_dir / _report_dir_for(suite_id, agent, tag)

    # 从任务注册表回填真实 category/difficulty/network（否则报告里是 unknown）
    result_task_ids = {r.task_id for r in current.runs} | {g.task_id for g in current.grades}
    task_meta = _report_task_meta(config, result_task_ids)

    paths = build_report(
        current,
        baseline,
        out_dir,
        formats=formats,
        baseline_report=baseline_report,
        task_meta=task_meta,
    )

    if console.json_mode:
        console.json(
            {
                "suite_id": suite_id,
                "agent": agent,
                "tag": tag,
                "out_dir": str(out_dir),
                "json": str(paths.json) if paths.json else None,
                "markdown": str(paths.markdown) if paths.markdown else None,
                "html": str(paths.html) if paths.html else None,
                "csv": str(paths.csv) if paths.csv else None,
                "warnings": list(getattr(paths, "warnings", []) or []),
            }
        )
    else:
        console.out(f"报告目录：{out_dir}")
        for fmt, p in (("json", paths.json), ("markdown", paths.markdown), ("html", paths.html), ("csv", paths.csv)):
            if p:
                console.out(f"  {fmt:9s} {p}")
        for w in getattr(paths, "warnings", []) or []:
            console.warn(w)
    return EXIT_OK


def _report_dir_for(suite_id: str, agent: str, tag: str) -> str:
    from .core.reporter import report_dir_name

    return report_dir_name(suite_id, agent, tag)


# --------------------------------------------------------------------------- #
# compare
# --------------------------------------------------------------------------- #
def cmd_compare(args: argparse.Namespace, console: Console) -> int:
    config = _load_config(args)
    store = ResultStore(config.results_dir)
    suite_id = args.suite or config.benchmark.default_suite
    agent, _ = _resolve_result_ref(store, config, suite_id, args.agent, args.baseline_tag)

    baseline = store.load_suite(suite_id, agent, args.baseline_tag)
    current = store.load_suite(suite_id, agent, args.current_tag)
    report = build_regression_report(baseline, current, gates=config.gates)

    if console.json_mode:
        console.json(report.to_dict())
    else:
        rows = []
        for delta in report.deltas:
            rows.append(
                [
                    delta.metric,
                    _fmt_float(delta.baseline),
                    _fmt_float(delta.current),
                    _fmt_float(delta.delta_pp),
                    delta.direction,
                ]
            )
        console.out(f"baseline={args.baseline_tag}  current={args.current_tag}  suite={suite_id}")
        console.out("")
        console.out(_table(rows, ["metric", "baseline", "current", "delta(pp)", "dir"]))
        console.out("")
        console.out(summarize_verdict(report.verdict))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# regression
# --------------------------------------------------------------------------- #
def cmd_regression(args: argparse.Namespace, console: Console) -> int:
    config = _load_config(args)
    store = ResultStore(config.results_dir)
    suite_id = args.suite or config.benchmark.default_suite
    agent, _ = _resolve_result_ref(store, config, suite_id, args.agent, args.baseline)

    baseline = store.load_suite(suite_id, agent, args.baseline)
    current = store.load_suite(suite_id, agent, args.current)
    strict_warn = bool(args.strict_warn or config.evaluation.strict_warn)
    report = build_regression_report(
        baseline, current, gates=config.gates, strict_warn=strict_warn
    )
    code = exit_code_for(report.verdict, strict_warn=strict_warn)

    if console.json_mode:
        payload = report.to_dict()
        payload["exit_code"] = code
        console.json(payload)
    else:
        if report.deltas:
            rows = []
            for delta in report.deltas:
                rows.append(
                    [
                        delta.metric,
                        _fmt_float(delta.baseline),
                        _fmt_float(delta.current),
                        _fmt_float(delta.delta_pp),
                        delta.direction,
                    ]
                )
            console.out(_table(rows, ["metric", "baseline", "current", "delta(pp)", "dir"]))
            console.out("")
        if report.gates:
            grows = []
            for gate in report.gates:
                if gate.threshold_delta_pp is not None:
                    threshold = _fmt_signed(gate.threshold_delta_pp) + "pp"
                elif gate.threshold_ratio is not None:
                    threshold = f"x{_fmt_float(gate.threshold_ratio, 2)}"
                else:
                    threshold = "-"
                grows.append(
                    [
                        gate.name,
                        threshold,
                        _fmt_float(gate.delta_pp) if gate.delta_pp is not None else "-",
                        gate.result,
                    ]
                )
            console.out(_table(grows, ["gate", "threshold", "delta(pp)", "result"]))
            console.out("")
        if report.newly_failed_tasks:
            console.out(f"新失败任务：{', '.join(report.newly_failed_tasks)}")
        if report.newly_passed_tasks:
            console.out(f"新通过任务：{', '.join(report.newly_passed_tasks)}")
        console.out(summarize_verdict(report.verdict))
    return code


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #
def cmd_validate(args: argparse.Namespace, console: Console) -> int:
    config = _load_config(args)
    from .core.registry import _dedupe_issues, discover_task_dirs, load_tasks_tolerant

    load_issues: list[ValidationIssue] = []
    if args.task_ids_file:
        tasks = _tasks_from_file(config, args.task_ids_file)
        suite_id = "(tasks-file)"
    elif args.suite:
        try:
            suite, tasks = _load_suite(config, args.suite)
            suite_id = args.suite
        except KaoyanBenchError as exc:
            console.error(str(exc))
            return EXIT_ERROR
        except Exception as exc:  # noqa: BLE001 - 兜底，避免崩栈
            console.error(
                f"加载 suite '{args.suite}' 失败（{type(exc).__name__}）：{exc}"
            )
            return EXIT_ERROR
    else:
        # 逐个加载：单个 task.json 不可加载时**收集为 issue 后继续**，
        # 保证其余任务的 schema 错误不会被吞掉（QA P1-1）。
        tasks, load_issues = load_tasks_tolerant(discover_task_dirs(config.tasks_root))
        suite_id = "(all)"

    issues: list[ValidationIssue] = list(load_issues)
    issues.extend(validate_tasks(tasks, snapshot_dir=config.snapshot_dir))
    issues.extend(validate_fixtures(tasks))
    # v1.1 快照收紧：--require-snapshots 把缺快照的 warn 提升为 error，
    # 用于发版门禁逐步收口（默认仍 warn，保证现有 20 个缺口不炸 CI）。
    if bool(getattr(args, "require_snapshots", False)):
        for issue in issues:
            if issue.level == "warn" and (issue.field or "") == "requires_snapshot":
                issue.level = "error"
                issue.message = "[require-snapshots] " + issue.message
    issues = _dedupe_issues(issues)

    errors = [i for i in issues if i.level == "error"]
    warns = [i for i in issues if i.level == "warn"]

    strict = bool(args.strict or config.evaluation.strict_warn)
    # 退出码约定：error → 2（错误）；仅 strict 下的 warn → 1（门禁不通过）；否则 0。
    if errors:
        exit_code = EXIT_ERROR
    elif strict and warns:
        exit_code = EXIT_GATE_FAIL
    else:
        exit_code = EXIT_OK
    failed = exit_code != EXIT_OK

    if console.json_mode:
        console.json(
            {
                "suite": suite_id,
                "task_count": len(tasks),
                "error_count": len(errors),
                "warn_count": len(warns),
                "passed": not failed,
                "strict": strict,
                "issues": [i.to_dict() for i in issues],
            }
        )
    else:
        console.out(f"suite={suite_id}  任务数={len(tasks)}  error={len(errors)}  warn={len(warns)}")
        for issue in issues:
            tag = "ERROR" if issue.level == "error" else "WARN "
            loc = f" [{issue.task_id}]" if issue.task_id else ""
            field = f" ({issue.field})" if issue.field else ""
            console.out(f"  {tag}{loc}{field} {issue.message}")
        console.out("")
        console.out("validate 通过 ✅" if not failed else "validate 未通过 ❌")

    return exit_code


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def cmd_fixtures(args: argparse.Namespace, console: Console) -> int:
    config = _load_config(args)
    from .core.registry import discover_task_dirs, load_tasks_tolerant

    task_ids = set(_split_csv(getattr(args, "task", None))) if getattr(args, "task", None) else None
    # ``fixtures check`` 子命令把 check 置真（``fixtures build --check`` 亦然）。
    check_mode = bool(getattr(args, "check", False))
    force = bool(getattr(args, "force", False))

    directories = [d for d in discover_task_dirs(config.tasks_root)
                   if not task_ids or d.name.upper() in {t.upper() for t in task_ids}]
    # 与 validate 一致：单个任务加载失败不阻断其余 fixture 校验。
    loaded, load_issues = load_tasks_tolerant(directories)

    report: list[dict[str, Any]] = []
    for issue in load_issues:
        report.append(
            {
                "task_id": issue.task_id,
                "task_dir": issue.file,
                "ok": False,
                "issues": [issue.to_dict()],
            }
        )
    for task in loaded:
        issues = validate_fixtures([task])
        report.append(
            {
                "task_id": task.task_id,
                "task_dir": task.task_dir,
                "ok": not any(i.level == "error" for i in issues),
                "issues": [i.to_dict() for i in issues],
            }
        )
    report.sort(key=lambda r: str(r["task_id"]))

    if check_mode:
        ok = all(r["ok"] for r in report)
        if console.json_mode:
            console.json({"check": True, "ok": ok, "tasks": report})
        else:
            for r in report:
                status = "OK" if r["ok"] else "FAIL"
                console.out(f"  {status}  {r['task_id']}")
            console.out("")
            console.out("fixtures 校验通过 ✅" if ok else "fixtures 校验未通过 ❌")
        return EXIT_OK if ok else EXIT_ERROR

    # build：调用任务集工程师提供的 tools/gen_fixtures.py（存在才执行）
    gen = config.root / "tools" / "gen_fixtures.py"
    if not gen.is_file():
        msg = (
            "fixtures build 依赖 tools/gen_fixtures.py（由任务集工程师负责，见 T-01/T-02）。"
            "当前不存在，跳过生成。"
        )
        if console.json_mode:
            console.json({"built": False, "reason": msg, "tasks": report})
        else:
            console.warn(msg)
        return EXIT_OK

    import subprocess

    argv = [sys.executable, str(gen)]
    if force:
        argv.append("--force")
    try:
        proc = subprocess.run(
            argv, cwd=str(config.root), capture_output=True, text=True, timeout=600
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        console.error(f"fixtures build 执行失败：{exc}")
        return EXIT_ERROR

    if console.json_mode:
        console.json(
            {
                "built": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:],
            }
        )
    else:
        console.out(proc.stdout.rstrip())
        if proc.stderr.strip():
            console.warn(proc.stderr.rstrip()[-2000:])
    return EXIT_OK if proc.returncode == 0 else EXIT_ERROR


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #
def cmd_snapshot_fetch(args: argparse.Namespace, console: Console) -> int:
    config = _load_config(args)
    store = SnapshotStore(config.snapshot_dir)
    task = _find_task(config, args.task)

    urls: list[str] = []
    if args.url:
        urls.extend(_split_csv(args.url))
    for req in task.expected.required_sources:
        # source requirements 里可能带 url 提示（存在则一并抓取）
        url = getattr(req, "url", None)
        if url:
            urls.append(str(url))
    if not urls:
        if task.task_dir:
            hint = Path(task.task_dir) / "urls.txt"
            if hint.is_file():
                urls = [
                    line.strip()
                    for line in hint.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
    if not urls:
        console.error(f"未提供 URL（--url）且任务 {task.task_id} 没有可抓取的来源提示")
        return EXIT_ERROR

    saved: list[dict[str, Any]] = []
    for url in urls:
        try:
            html, status, final_url = fetch_url(url)
        except KaoyanBenchError as exc:
            console.warn(f"抓取失败 {url}：{exc}")
            continue
        page = store.save_page(
            task.task_id,
            final_url,
            html,
            title=_page_title(html),
            http_status=status,
        )
        saved.append({"url": page.url, "snapshot_path": page.snapshot_path, "sha256": page.content_sha256})

    if console.json_mode:
        console.json({"task_id": task.task_id, "saved": saved})
    else:
        console.out(f"任务 {task.task_id}：已保存 {len(saved)} 个快照到 {config.snapshot_dir}")
        for s in saved:
            console.out(f"  {s['url']}")
    return EXIT_OK


def _page_title(html: bytes) -> str:
    try:
        text = html.decode("utf-8", errors="ignore")
    except Exception:  # pragma: no cover
        return ""
    import re

    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else ""


def cmd_snapshot_verify(args: argparse.Namespace, console: Console) -> int:
    config = _load_config(args)
    store = SnapshotStore(config.snapshot_dir)
    task_id = args.task if args.task and args.task != "all" else None

    issues = store.verify(task_id)
    errors = [i for i in issues if getattr(i, "level", "error") == "error"]

    if console.json_mode:
        console.json(
            {
                "task_id": task_id,
                "checked": len(store.pages(task_id)) if task_id else None,
                "issue_count": len(issues),
                "issues": [_issue_dict(i) for i in issues],
            }
        )
    else:
        if not issues:
            console.out("快照校验通过 ✅")
        else:
            for i in issues:
                console.out(f"  {getattr(i, 'level', 'error').upper()}  {_issue_dict(i)}")
            console.out("")
            console.out("快照校验未通过 ❌" if errors else "快照校验通过（有告警）")
    return EXIT_ERROR if errors else EXIT_OK


def _issue_dict(issue: Any) -> dict[str, Any]:
    if hasattr(issue, "to_dict"):
        return issue.to_dict()
    if hasattr(issue, "__dict__"):
        return {k: v for k, v in vars(issue).items()}
    return {"message": str(issue)}


# --------------------------------------------------------------------------- #
# suite / agent（辅助列举命令）
# --------------------------------------------------------------------------- #
def cmd_suite(args: argparse.Namespace, console: Console) -> int:
    config = _load_config(args)
    store = ResultStore(config.results_dir)
    suites = store.list_suites(suite_id=args.suite)
    if console.json_mode:
        console.json({"suites": suites})
    else:
        if not suites:
            console.out("（尚无已运行的 suite 结果）")
        else:
            rows = [
                [s.get("suite_id"), s.get("agent"), s.get("tag"), s.get("task_count"), s.get("grader_mode"), s.get("created_at")]
                for s in suites
            ]
            console.out(_table(rows, ["suite", "agent", "tag", "tasks", "grader_mode", "created_at"]))
    return EXIT_OK


def cmd_agent(args: argparse.Namespace, console: Console) -> int:
    config = _load_config(args)
    names = list_agent_names(config)
    specs = []
    for name in names:
        try:
            spec = load_agent_spec(name, config=config)
            specs.append(
                {
                    "name": name,
                    "type": spec.type,
                    "version": spec.version,
                    "model": spec.model,
                    "provider": spec.provider,
                    "source": spec.source_path,
                }
            )
        except KaoyanBenchError as exc:  # pragma: no cover
            specs.append({"name": name, "error": str(exc)})
    if console.json_mode:
        console.json({"agents": specs})
    else:
        rows = [[s.get("name"), s.get("type"), s.get("version"), s.get("model"), s.get("provider")] for s in specs]
        console.out(_table(rows, ["agent", "type", "version", "model", "provider"]))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def _split_csv(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for v in value:
            out.extend(_split_csv(v))
        return out
    return [x.strip() for x in str(value).split(",") if x.strip()]


def _add_global(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-c", "--config", help="配置文件路径（默认 config/default.yaml）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志到 stderr")
    parser.add_argument("--quiet", action="store_true", help="静默模式（不输出日志）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（stdout 仅合法 JSON）")
    parser.add_argument("--root", help="项目根目录（默认自动探测）")


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    """在子命令上补 ``--json``（方案 5.10 把 ``[--json]`` 写在子命令行）。

    使用独立 dest ``json_sub``：这样既保留全局 ``--json``，又能让子命令级的
    ``--json`` 生效，且二者互不覆盖。最终在 :func:`main` 中取「或」合并
    （QA P2-1）。
    """
    parser.add_argument(
        "--json",
        dest="json_sub",
        action="store_true",
        default=False,
        help="以 JSON 输出（stdout 仅合法 JSON）；等价于全局 --json",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="KaoyanBench v1.0 —— 考研场景 AI Agent 评测基准（核心路径零第三方依赖）",
    )
    parser.add_argument("--version", action="version", version=__version__)
    _add_global(parser)

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # list
    p = sub.add_parser("list", help="列出任务（可按 suite/split/category/difficulty/tag 过滤）")
    p.add_argument("--suite")
    p.add_argument("--tasks-file", dest="task_ids_file")
    p.add_argument("--split", choices=["public", "private"])
    p.add_argument("--category")
    p.add_argument("--difficulty")
    p.add_argument("--tag")
    p.add_argument("--show-missing-snapshot", action="store_true")
    _add_json_flag(p)
    p.set_defaults(func=cmd_list)

    # run
    p = sub.add_parser("run", help="运行评测（写入 results/ 并打印摘要）")
    p.add_argument("--suite")
    p.add_argument("--task", help="单个或多个 task_id（逗号分隔）")
    p.add_argument("--tasks-file", dest="tasks_file", help="每行一个 task_id 的文件")
    p.add_argument("--agent", required=True, help="Agent 名或配置路径（如 mock / echo / kaoyan_chain）")
    p.add_argument("-A", "--agent-config", dest="agent_config", help="显式指定 Agent 配置路径")
    p.add_argument("--split")
    p.add_argument("--category")
    p.add_argument("--difficulty")
    p.add_argument("--tag")
    p.add_argument("--runs", type=int, help="每个任务的重复次数")
    p.add_argument("--seed", type=int, help="随机种子（保证可复现）")
    p.add_argument("--concurrency", type=int, help="并发数（默认 1）")
    p.add_argument("--offline-replay", action="store_true", help="离线回放快照，不发起任何网络请求")
    p.add_argument("--time-limit", type=int, help="覆盖任务的 time_limit（秒）")
    p.add_argument("--tool-limit", type=int, help="覆盖任务的 tool_limit")
    p.add_argument("--keep-workspace", action="store_true", help="运行后保留 workspace")
    p.add_argument("--fail-fast", action="store_true", help="首个失败即停止")
    _add_json_flag(p)
    p.set_defaults(func=cmd_run)

    # grade
    p = sub.add_parser("grade", help="对已跑过的 run 重新评分（P1）")
    p.add_argument("--run-id", required=True)
    p.add_argument("--grader")
    _add_json_flag(p)
    p.set_defaults(func=cmd_grade)

    # report
    p = sub.add_parser("report", help="生成报告（json/markdown/html/csv）")
    p.add_argument("--suite")
    p.add_argument("--agent")
    p.add_argument("--tag")
    p.add_argument("-f", "--format", help="输出格式，逗号分隔（json,markdown,html,csv）")
    p.add_argument("--baseline", help="baseline tag（可选，生成对比章节）")
    p.add_argument("-o", "--out", help="输出目录")
    _add_json_flag(p)
    p.set_defaults(func=cmd_report)

    # compare
    p = sub.add_parser("compare", help="对比两个 tag 的指标（Δpp）")
    p.add_argument("baseline_tag")
    p.add_argument("current_tag")
    p.add_argument("--suite")
    p.add_argument("--agent")
    _add_json_flag(p)
    p.set_defaults(func=cmd_compare)

    # regression
    p = sub.add_parser("regression", help="回归门禁（0 pass / 1 gate fail / 2 错误）")
    p.add_argument("--baseline", required=True)
    p.add_argument("--current", required=True)
    p.add_argument("--suite")
    p.add_argument("--agent")
    p.add_argument("--strict-warn", action="store_true", help="warn 也视为失败（退出码 1）")
    _add_json_flag(p)
    p.set_defaults(func=cmd_regression)

    # validate
    p = sub.add_parser("validate", help="校验任务 schema 与 fixture hash")
    p.add_argument("--suite")
    p.add_argument("--tasks-file", dest="task_ids_file")
    p.add_argument("--strict", action="store_true", help="warn 也视为不通过")
    p.add_argument(
        "--require-snapshots",
        action="store_true",
        help="联网/快照任务缺 snapshots/<task_id>/index.json 时视为 error（v1.1 收紧快照缺口用）",
    )
    _add_json_flag(p)
    p.set_defaults(func=cmd_validate)

    # fixtures（5.10 契约：build [--task] [--force] [--check]；并补 check 子命令）
    p = sub.add_parser("fixtures", help="fixtures 生成与校验")
    fsub = p.add_subparsers(dest="fixtures_command", metavar="<build|check>")
    fb = fsub.add_parser("build", help="生成 fixtures（调用 tools/gen_fixtures.py；--check 等价于校验）")
    fb.add_argument("--task")
    fb.add_argument("--force", action="store_true")
    fb.add_argument("--check", action="store_true", help="不生成，仅校验现有 fixtures（等价于 fixtures check）")
    _add_json_flag(fb)
    fb.set_defaults(func=cmd_fixtures, check=False, force=False)
    fc = fsub.add_parser("check", help="校验 fixtures（manifest 内文件存在且 sha256 匹配）")
    fc.add_argument("--task")
    _add_json_flag(fc)
    fc.set_defaults(func=cmd_fixtures, check=True, force=False)

    # snapshot
    p = sub.add_parser("snapshot", help="网页快照抓取与校验")
    ssub = p.add_subparsers(dest="snapshot_command", metavar="<fetch|verify>")
    sf = ssub.add_parser("fetch", help="抓取任务来源页快照（需联网，P1）")
    sf.add_argument("--task", required=True)
    sf.add_argument("--url", help="显式 URL（逗号分隔）")
    _add_json_flag(sf)
    sf.set_defaults(func=cmd_snapshot_fetch)
    sv = ssub.add_parser("verify", help="校验快照 hash")
    sv.add_argument("--task", default="all")
    _add_json_flag(sv)
    sv.set_defaults(func=cmd_snapshot_verify)

    # suite
    p = sub.add_parser("suite", help="列出已运行的 suite 结果")
    p.add_argument("--suite")
    _add_json_flag(p)
    p.set_defaults(func=cmd_suite)

    # agent
    p = sub.add_parser("agent", help="列出可用 Agent 配置")
    _add_json_flag(p)
    p.set_defaults(func=cmd_agent)

    return parser


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK

    console = Console(
        json_mode=bool(getattr(args, "json", False)) or bool(getattr(args, "json_sub", False)),
        quiet=bool(getattr(args, "quiet", False)),
        verbose=bool(getattr(args, "verbose", False)),
    )
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return EXIT_ERROR

    try:
        return int(func(args, console))
    except KaoyanBenchError as exc:
        # 领域异常：可读信息，不含堆栈/敏感内容
        console.error(str(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        console.error("已中断")
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底，避免裸堆栈
        console.error(f"未预期的错误：{type(exc).__name__}: {exc}")
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc(file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
