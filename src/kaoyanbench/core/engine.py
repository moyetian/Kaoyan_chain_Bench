"""评测引擎：把「任务 → Runner → 日志 → Grader → 聚合 → SuiteResult」串起来。

- **可复现性**：所有时间戳 / 随机 id 都由外部注入（``now`` / ``run_id_factory``）；
  评分路径本身不读时钟。默认的注入器只在 CLI 入口使用。
- **隔离**：每个 task × attempt 一个独立 workspace；环境变量白名单；
  成功清理 / 失败保留（``--keep-workspace`` 一律保留）。
- **铁律 2**：单个任务崩溃不拖垮整轮（除非 ``fail_fast``）。
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..utils.hashing import sha256_text
from ..utils.timex import utcnow_iso
from .config import Config
from .errors import ErrorCode, KaoyanBenchError
from .grader import GradeContext, get_grader
from .logger import RunLogger
from .metrics import aggregate
from .models import (
    AgentOutput,
    Environment,
    ErrorItem,
    GradeResult,
    Run,
    SuiteResult,
    Task,
)
from .registry import index_tasks
from .reporter import build_report  # noqa: F401 - 便于外部 from engine import ...
from .runner import get_runner, runner_environment, usage_missing
from .sandbox import Sandbox
from .scorer import finalize_grade
from .store import ResultStore
from .workspace import (
    Workspace,
    cleanup_workspace,
    prepare_workspace,
)

__all__ = [
    "EngineResult",
    "RunSpec",
    "run_suite",
    "run_single_task",
    "build_suite_result",
    "default_run_id_factory",
    "TaskOutcome",
]


# --------------------------------------------------------------------------- #
# run_id
# --------------------------------------------------------------------------- #
def default_run_id_factory(now: str | None = None) -> str:
    """``run_<UTC时间戳>_<6位随机>``（方案 4.3）。"""
    stamp = (now or utcnow_iso()).replace("-", "").replace(":", "").replace("+", "")
    stamp = stamp.split(".")[0].replace("T", "T")[:15]
    return f"run_{stamp}_{secrets.token_hex(3)}"


@dataclass
class TaskOutcome:
    """单个 task × attempt 的完整结果。"""

    task: Task
    run: Run
    grade: GradeResult
    workspace: Workspace | None = None
    log_path: Path | None = None
    stdout: str = ""
    stderr: str = ""

    @property
    def success(self) -> bool:
        return bool(self.grade.metrics.task_success)


@dataclass
class EngineResult:
    """整轮运行的产物。"""

    suite_result: SuiteResult
    outcomes: list[TaskOutcome] = field(default_factory=list)
    run_paths: list[Path] = field(default_factory=list)
    suite_path: Path | None = None
    report_paths: Any | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def runs(self) -> list[Run]:
        return [o.run for o in self.outcomes]

    @property
    def grades(self) -> list[GradeResult]:
        return [o.grade for o in self.outcomes]


@dataclass
class RunSpec:
    """一次 ``run`` 调用的全部参数（取代长参数列表，便于 CLI 与测试共用）。"""

    tasks: Sequence[Task]
    suite_id: str
    split: str = "public"
    agent_name: str = "mock"
    agent_spec: Any | None = None
    tag: str = "v1"
    runs_per_task: int = 1
    seed: int | None = 42
    concurrency: int = 1
    time_limit: int | None = None
    tool_limit: int | None = None
    # suite.defaults 提供的兜底值（优先级低于 task 级声明，见 run_single_task）
    suite_time_limit: int | None = None
    suite_tool_limit: int | None = None
    offline_replay: bool = False
    keep_workspace: bool = False
    fail_fast: bool = False
    benchmark_version: str = "1.0"


def _task_meta(tasks: Iterable[Task]) -> dict[str, dict[str, Any]]:
    return {
        t.task_id: {
            "category": t.category,
            "difficulty": t.difficulty,
            "network": t.network,
        }
        for t in tasks
    }


# --------------------------------------------------------------------------- #
# 单任务
# --------------------------------------------------------------------------- #
def run_single_task(
    task: Task,
    *,
    config: Config,
    agent_spec: Any,
    suite_id: str,
    attempt: int,
    seed: int | None,
    tag: str,
    run_id: str,
    now: str | None = None,
    time_limit: int | None = None,
    tool_limit: int | None = None,
    suite_time_limit: int | None = None,
    suite_tool_limit: int | None = None,
    offline_replay: bool = False,
    keep_workspace: bool = False,
    store: ResultStore | None = None,
    semantic_client: Any | None = None,
    source_levels: Mapping[str, Any] | None = None,
    fail_fast: bool = False,
    benchmark_version: str = "1.0",
) -> TaskOutcome:
    """跑一次任务：workspace → Runner → 日志 → Grader → 落盘。"""
    started_at = now or utcnow_iso()
    # 优先级（v1.1 修正）：CLI 显式覆盖 > suite.defaults > 任务级声明 > 全局配置。
    # 理由：suite.defaults 是本轮运行的预算帽（如 smoke 用 120s/30
    # 控制 CI 耗时，core50 用 900s/100 给 research 长任务空间）；若任务优先，
    # suite.defaults 将永远死码（任务总带默认值 900/60）。任务特调仍可经 CLI 覆盖。
    effective_time_limit = int(
        time_limit or suite_time_limit or task.time_limit or config.runtime.timeout or 900
    )
    effective_tool_limit = int(
        tool_limit or suite_tool_limit or task.tool_limit or config.runtime.max_tool_calls or 60
    )

    workspace = prepare_workspace(config.workspace_root, run_id, task)
    logger = RunLogger(
        (store.run_path(run_id) if store else Path(config.results_dir) / "runs" / f"{run_id}.jsonl"),
        run_id=run_id,
    )

    run = Run(
        run_id=run_id,
        suite_id=suite_id,
        attempt=attempt,
        seed=seed,
        agent=agent_spec.name or agent_spec.type,
        agent_version=agent_spec.version,
        model=agent_spec.model or "",
        model_version=None,
        provider=agent_spec.provider,
        task_id=task.task_id,
        started_at=started_at,
        tag=tag,
        offline_replay=offline_replay,
        environment=runner_environment(),
        time_limit=effective_time_limit,
        tool_limit=effective_tool_limit,
    )
    logger.run_start(run, task_meta={"category": task.category, "difficulty": task.difficulty})

    runner = get_runner(agent_spec)
    runner.fail_fast = bool(fail_fast)

    ctx = _build_run_context(
        task,
        workspace,
        run_id=run_id,
        attempt=attempt,
        seed=seed,
        time_limit=effective_time_limit,
        tool_limit=effective_tool_limit,
        config=config,
        offline_replay=offline_replay,
    )

    output: AgentOutput
    try:
        output = runner.run(task, ctx)
    except KaoyanBenchError:
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底（fail_fast 由 runner 处理）
        output = AgentOutput(
            run_id=run_id,
            agent=agent_spec.name,
            task_id=task.task_id,
            started_at=started_at,
            ended_at=utcnow_iso(),
            final_answer="",
            errors=[
                ErrorItem(
                    code=ErrorCode.UNKNOWN.value,
                    stage="runner",
                    message=f"Runner 未捕获异常（{type(exc).__name__}）",
                    task_id=task.task_id,
                    run_id=run_id,
                    at=utcnow_iso(),
                )
            ],
        )

    # 落日志（边跑边写已在 Runner 内部完成；这里补齐 tool_call / error / run_end）
    for index, call in enumerate(output.tool_calls or []):
        logger.tool_call(call, index=index)
    for item in output.errors or []:
        logger.error(item)

    grade_ctx = GradeContext(
        task_dir=Path(task.task_dir) if task.task_dir else config.tasks_root,
        workspace_dir=workspace.root,
        task_id=task.task_id,
        snapshot_dir=config.snapshot_dir,
        offline_replay=offline_replay,
        semantic_client=semantic_client,
        source_levels=dict(source_levels or {}),
        weights=dict(config.evaluation.weights),
        now=started_at,
        tool_limit=effective_tool_limit,
        time_limit=effective_time_limit,
        pass_threshold=float(task.grader.pass_threshold or config.evaluation.pass_threshold),
        grader_type=task.grader.type,
        write_human_review=(task.grader.type == "manual"),
        reports_dir=config.reports_dir,
    )

    try:
        grade = get_grader(task).grade(task, output, grade_ctx)
    except KaoyanBenchError as exc:
        # ScoringError 等评分异常：**降级为 FAIL 结果**，不让任务从结果集中消失。
        # （铁律 2：单个任务出错不拖垮整轮；同时保证跑过的任务必有一条记录）
        grade = _fallback_grade(
            task,
            output,
            ctx=grade_ctx,
            reason=str(exc),
        )

    # Run 落库/落盘
    run = _to_run(
        output,
        run,
        grade=grade,
        stdout=output.stdout,
        stderr=output.stderr,
        results_dir=config.results_dir,
    )
    logger.run_end(run, stdout=output.stdout, stderr=output.stderr)
    logger.close()
    if store is not None:
        store.write_run(run)
        store.write_grade(grade)

    if not keep_workspace:
        cleanup_workspace(
            workspace,
            keep=False,
            reason="success" if grade.metrics.task_success else "failure",
        )

    return TaskOutcome(
        task=task,
        run=run,
        grade=grade,
        workspace=workspace,
        log_path=logger.path,
        stdout=output.stdout,
        stderr=output.stderr,
    )


def _fallback_grade(
    task: Task,
    output: AgentOutput,
    *,
    ctx: Any,
    reason: str,
) -> GradeResult:
    """评分阶段抛错时的兜底 GradeResult（score=0、task_success=False、附 UNKNOWN 错误）。

    保证「跑过的任务必有一条可聚合的 FAIL 记录」，不因评分异常而从结果集消失。
    """
    from ..utils.timex import now_iso
    from .models import ScoreBreakdown
    from .scorer import compute_metrics

    at = now_iso(getattr(ctx, "now", None))
    grade = GradeResult(
        task_id=task.task_id,
        run_id=output.run_id,
        grader_type=getattr(ctx, "grader_type", task.grader.type) or task.grader.type,
        grader_mode="degraded",
        degraded_reason=f"评分异常，已降级为 FAIL：{reason}",
        score=ScoreBreakdown(),
        checks=[],
        graded_at=at,
    )
    grade.score.total = 0.0
    grade.errors = list(output.errors or []) + [
        ErrorItem(
            code=ErrorCode.UNKNOWN.value,
            stage="grade",
            message="评分阶段异常，该任务计为 FAIL（详见 degraded_reason）",
            task_id=task.task_id,
            run_id=output.run_id,
            at=at,
        )
    ]
    grade.metrics = compute_metrics(task, output, grade, checks=[], pass_threshold=float(
        task.grader.pass_threshold or getattr(ctx, "pass_threshold", 60.0)
    ))
    grade.metrics.task_success = False
    return grade


def _build_run_context(
    task: Task,
    workspace: Workspace,
    *,
    run_id: str,
    attempt: int,
    seed: int | None,
    time_limit: int,
    tool_limit: int,
    config: Config,
    offline_replay: bool,
) -> Any:
    from .models import RunContext

    sandbox = Sandbox(
        timeout_grace_sec=config.runtime.timeout_grace_sec,
        env_passthrough=config.runtime.env_passthrough,
        docker=config.runtime.docker,
    )
    env = sandbox.build_env(
        workspace,
        replay_dir=config.snapshot_dir if offline_replay else None,
    )
    return RunContext(
        run_id=run_id,
        attempt=attempt,
        seed=seed,
        task_dir=Path(task.task_dir) if task.task_dir else config.tasks_root,
        workspace_dir=workspace.root,
        snapshot_dir=config.snapshot_dir if (offline_replay or task.requires_snapshot) else None,
        offline_replay=offline_replay,
        time_limit=time_limit,
        tool_limit=tool_limit,
        env=env,
        usage_file=workspace.usage_file,
        answer_file=workspace.answer_file,
    )


def _to_run(
    output: AgentOutput,
    run: Run,
    *,
    grade: GradeResult,
    stdout: str,
    stderr: str,
    results_dir: Path,
) -> Run:
    """把 ``AgentOutput`` 合并进 ``Run``（保留 run 的 suite/attempt 等上下文）。"""
    from .models import Citation, Source, ToolCall

    merged = Run.from_dict(run.to_dict())
    merged.ended_at = output.ended_at or utcnow_iso()
    merged.duration_ms = int(output.duration_ms or 0)
    merged.tool_calls_detail = list(output.tool_calls or [])
    merged.tool_calls = len(merged.tool_calls_detail)
    merged.sources = list(output.sources or [])
    merged.citations = list(output.citations or [])
    merged.final_answer = output.final_answer or ""
    merged.answer_files = dict(output.answer_files or {})
    merged.errors = list(output.errors or [])
    merged.usage = output.usage
    merged.timed_out = bool(output.timed_out)
    merged.exit_code = output.exit_code
    merged.search_trace = output.search_trace
    merged.success = output.success
    merged.offline_replay = run.offline_replay
    merged.grader_mode = grade.grader_mode
    merged.model = output.model or run.model
    merged.model_version = output.model_version
    merged.provider = output.provider or run.provider
    merged.agent = output.agent or run.agent
    merged.agent_version = output.agent_version or run.agent_version
    merged.environment = output.environment or run.environment
    merged.stdout_sha256 = sha256_text(stdout) if stdout else None
    merged.stderr_sha256 = sha256_text(stderr) if stderr else None
    # 产出文件原始文本存盘（便于 grade --run-id 复评）；stdout_path 相对 results_dir
    _persist_raw(results_dir, merged.run_id, stdout, stderr)
    merged.stdout_path = str(Path("outputs") / f"{merged.run_id}.stdout.txt")
    return merged


def _persist_raw(results_dir: Path, run_id: str, stdout: str, stderr: str) -> None:
    """把原始 stdout/stderr 存到 ``<results_dir>/outputs/``（库内只留 hash 与相对路径）。"""
    target = Path(results_dir) / "outputs"
    target.mkdir(parents=True, exist_ok=True)
    try:
        (target / f"{run_id}.stdout.txt").write_text(stdout or "", encoding="utf-8")
        (target / f"{run_id}.stderr.txt").write_text(stderr or "", encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 整轮
# --------------------------------------------------------------------------- #
def run_suite(
    spec: RunSpec,
    *,
    config: Config,
    store: ResultStore | None = None,
    agent_spec: Any | None = None,
    semantic_client: Any | None = None,
    source_levels: Mapping[str, Any] | None = None,
    now: str | None = None,
    log: Callable[[str], None] | None = None,
    run_id_factory: Callable[[str | None], str] | None = None,
    task_meta: Mapping[str, Mapping[str, Any]] | None = None,
) -> EngineResult:
    """跑一个 suite（多任务 × 多次重复），返回 :class:`EngineResult`。

    ``run_id`` 通过 ``run_id_factory(now)`` 注入，保证测试可完全确定。
    """
    emit = log or (lambda _msg: None)
    factory = run_id_factory or default_run_id_factory
    tasks = sorted(spec.tasks, key=lambda t: t.task_id)
    if not tasks:
        raise KaoyanBenchError("没有可运行的任务：请检查 --suite / --task / 过滤条件")

    agent = agent_spec or spec.agent_spec
    if agent is None:
        raise KaoyanBenchError("缺少 AgentSpec，无法运行")
    store = store or ResultStore(config.results_dir)

    semaphore = threading.Semaphore(max(1, int(spec.concurrency)))
    outcomes: list[TaskOutcome] = []
    warnings: list[str] = []
    base_now = now or utcnow_iso()

    jobs: list[tuple[Task, int]] = [
        (task, attempt)
        for task in tasks
        for attempt in range(1, max(1, spec.runs_per_task) + 1)
    ]

    def _execute(task: Task, attempt: int) -> TaskOutcome:
        run_id = factory(base_now)
        with semaphore:
            return run_single_task(
                task,
                config=config,
                agent_spec=agent,
                suite_id=spec.suite_id,
                attempt=attempt,
                seed=spec.seed,
                tag=spec.tag,
                run_id=run_id,
                now=base_now,
                time_limit=spec.time_limit,
                tool_limit=spec.tool_limit,
                suite_time_limit=spec.suite_time_limit,
                suite_tool_limit=spec.suite_tool_limit,
                offline_replay=spec.offline_replay,
                keep_workspace=spec.keep_workspace,
                store=store,
                semantic_client=semantic_client,
                source_levels=source_levels,
                fail_fast=spec.fail_fast,
                benchmark_version=spec.benchmark_version,
            )

    if spec.concurrency > 1 and len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=min(4, spec.concurrency)) as pool:
            futures = {pool.submit(_execute, task, attempt): (task, attempt) for task, attempt in jobs}
            for future in as_completed(futures):
                task, attempt = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001
                    if spec.fail_fast:
                        raise
                    warnings.append(
                        f"{task.task_id} 第 {attempt} 次运行失败（{type(exc).__name__}）"
                    )
                    continue
                outcomes.append(outcome)
                emit(_progress_line(outcome))
    else:
        for task, attempt in jobs:
            try:
                outcome = _execute(task, attempt)
            except Exception as exc:  # noqa: BLE001
                if spec.fail_fast:
                    raise
                warnings.append(f"{task.task_id} 第 {attempt} 次运行失败（{type(exc).__name__}）")
                continue
            outcomes.append(outcome)
            emit(_progress_line(outcome))

    outcomes.sort(key=lambda o: (o.task.task_id, o.run.attempt, o.run.run_id))

    suite_result = build_suite_result(
        outcomes,
        spec=spec,
        config=config,
        now=base_now,
        task_meta=task_meta,
    )
    suite_path = store.write_suite(suite_result, spec.tag)
    try:
        store.sync_db()
    except Exception:  # noqa: BLE001 - DB 是派生数据，同步失败不影响主流程
        warnings.append("SQLite 同步失败（DB 为派生数据，可稍后重建）")

    return EngineResult(
        suite_result=suite_result,
        outcomes=outcomes,
        run_paths=[o.log_path for o in outcomes if o.log_path],
        suite_path=suite_path,
        warnings=warnings,
    )


def _progress_line(outcome: TaskOutcome) -> str:
    flag = "PASS" if outcome.success else "FAIL"
    return (
        f"  {outcome.task.task_id:<14} {flag}  "
        f"score={outcome.grade.score_total:6.2f}  "
        f"mode={outcome.grade.grader_mode:<8} "
        f"latency={outcome.run.duration_ms / 1000:.2f}s"
    )


def _git_sha() -> str | None:
    """最佳努力获取 git commit（对标 MASEval/Inspect 可复现要求：报告含 git 状态）。

    非 git 目录或 git 不可用时返回 None，不让评测崩掉。
    """
    try:
        import subprocess

        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        sha = (out.stdout or "").strip()
        return sha or None
    except Exception:  # noqa: BLE001 - 可复现元数据缺失不应阻断评测
        return None


def build_suite_result(
    outcomes: Sequence[TaskOutcome],
    *,
    spec: RunSpec,
    config: Config,
    now: str,
    task_meta: Mapping[str, Mapping[str, Any]] | None = None,
) -> SuiteResult:
    """把 outcomes 聚合成 :class:`SuiteResult`（含 aggregates / score_breakdown_mean）。"""
    runs = [o.run for o in outcomes]
    grades = [o.grade for o in outcomes]
    tasks = [o.task for o in outcomes]
    meta = dict(task_meta or _task_meta(tasks))

    aggregates = aggregate(
        grades,
        runs,
        task_meta=meta,
        include_groups=True,
    )
    from .metrics import score_breakdown_mean

    return SuiteResult(
        schema_version="1.0",
        suite_id=spec.suite_id,
        split=spec.split,
        task_count=len({t.task_id for t in tasks}),
        agent=outcomes[0].run.agent if outcomes else (spec.agent_name or ""),
        agent_version=outcomes[0].run.agent_version if outcomes else None,
        model=outcomes[0].run.model if outcomes else "",
        runs_per_task=max(1, int(spec.runs_per_task)),
        seed=spec.seed,
        tag=spec.tag,
        created_at=now,
        offline_replay=spec.offline_replay,
        environment=outcomes[0].run.environment if outcomes else runner_environment(),
        runs=runs,
        grades=grades,
        aggregates=aggregates,
        score_breakdown_mean={
            k: v for k, v in score_breakdown_mean(grades).items() if v is not None
        },
        source={
            "benchmark_version": spec.benchmark_version,
            "config_path": str(config.path) if config.path else None,
            "results_dir": str(config.results_dir),
            "concurrency": spec.concurrency,
            "git_sha": _git_sha(),
            "trace_schema_version": "1.0",
        },
    )


def grade_existing_run(
    run: Run,
    task: Task,
    *,
    config: Config,
    store: ResultStore,
    semantic_client: Any | None = None,
    source_levels: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> GradeResult:
    """对**已跑过**的 run 重新评分（``grade --run-id``，P1）。"""
    from .models import AgentOutput as _AgentOutput

    output = _AgentOutput(
        run_id=run.run_id,
        agent=run.agent,
        agent_version=run.agent_version,
        model=run.model,
        task_id=run.task_id,
        started_at=run.started_at,
        ended_at=run.ended_at,
        duration_ms=run.duration_ms,
        final_answer=run.final_answer,
        answer_files=dict(run.answer_files),
        tool_calls=list(run.tool_calls_detail),
        sources=list(run.sources),
        citations=list(run.citations),
        search_trace=run.search_trace,
        errors=list(run.errors),
        usage=run.usage,
        timed_out=run.timed_out,
        exit_code=run.exit_code,
        raw=None,
        environment=run.environment,
    )
    workspace_dir = config.workspace_root / run.run_id
    # 口径统一（v1.1）：复评沿用首评实际生效预算；老数据缺字段时回退任务声明值。
    re_time_limit = int(run.time_limit or task.time_limit or 900)
    re_tool_limit = int(run.tool_limit or task.tool_limit or 60)
    ctx = GradeContext(
        task_dir=Path(task.task_dir) if task.task_dir else config.tasks_root,
        workspace_dir=workspace_dir,
        task_id=task.task_id,
        snapshot_dir=config.snapshot_dir,
        offline_replay=run.offline_replay,
        semantic_client=semantic_client,
        source_levels=dict(source_levels or {}),
        weights=dict(config.evaluation.weights),
        now=now or utcnow_iso(),
        tool_limit=re_tool_limit,
        time_limit=re_time_limit,
        pass_threshold=float(task.grader.pass_threshold or config.evaluation.pass_threshold),
        grader_type=task.grader.type,
        write_human_review=(task.grader.type == "manual"),
        reports_dir=config.reports_dir,
    )
    grade = get_grader(task).grade(task, output, ctx)
    store.write_grade(grade)
    return grade


def export_run_as_json(run: Run) -> str:
    """调试用：Run → JSON 文本。"""
    return json.dumps(run.to_dict(), ensure_ascii=False, indent=2)


def summary_line(suite_result: SuiteResult) -> str:
    """一行人类可读摘要（``run`` 命令末尾打印）。"""
    agg = suite_result.aggregates
    rate = "n/a" if agg.task_success_rate is None else f"{agg.task_success_rate:.1%}"
    mean = "n/a" if agg.score_mean is None else f"{agg.score_mean:.2f}"
    return (
        f"suite={suite_result.suite_id} tag={suite_result.tag} "
        f"agent={suite_result.agent} tasks={agg.n_tasks} "
        f"success_rate={rate} score_mean={mean} grader_mode={agg.grader_mode}"
    )


def _unused(*_: Any) -> None:  # pragma: no cover
    _ = (index_tasks, usage_missing, os)
