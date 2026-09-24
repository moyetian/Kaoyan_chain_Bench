"""Grader 协议与注册表（方案 5.3 / B-13 ~ B-16）。

- ``GradeContext`` 是评分所需的**全部外部输入**（含注入的 ``now``，保证可复现）。
- ``get_grader(task)`` 按 ``task.grader.type`` 返回具体 Grader。
- 所有 Grader 必须遵守：**不允许静默假装评过**（降级必须显式标注）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..utils.timex import now_iso
from .checks import run_check
from .errors import GraderError
from .evidence import EvidenceRules, judge_sources
from .models import (
    DEFAULT_WEIGHTS,
    AgentOutput,
    Check,
    CheckResult,
    GradeResult,
    GraderSpec,
    Metrics,
    ScoreBreakdown,
    Task,
)

__all__ = [
    "GradeContext",
    "Grader",
    "get_grader",
    "register_grader",
    "registered_graders",
    "BaseGrader",
    "DEGRADED_REASONS",
    "resolve_weights",
    "run_checks",
    "attach_evidence_levels",
]

#: 降级原因枚举（方案 5.3 降级规则表）。
DEGRADED_REASONS: tuple[str, ...] = (
    "no_api_key",
    "network_unreachable",
    "invalid_response",
    "semantic_disabled",
    "not_configured",
)


@dataclass
class GradeContext:
    """评分上下文（方案 5.3，字段与契约一致）。"""

    task_dir: Path
    workspace_dir: Path
    task_id: str = ""
    snapshot_dir: Path | None = None
    offline_replay: bool = False
    semantic_client: Any | None = None
    source_levels: dict[str, Any] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    now: str = ""
    tool_limit: int = 60
    time_limit: int = 900
    pass_threshold: float = 60.0
    grader_type: str | None = None
    write_human_review: bool = False
    reports_dir: Path | None = None

    def __post_init__(self) -> None:
        if not self.now:
            self.now = now_iso(None)
        self.task_dir = Path(self.task_dir)
        self.workspace_dir = Path(self.workspace_dir)
        if not self.task_id:
            # 回退：任务目录名即 task_id（与 registry.discover_task_dirs 的约定一致）
            self.task_id = self.task_dir.name
        if self.snapshot_dir is not None:
            self.snapshot_dir = Path(self.snapshot_dir)
        if not self.weights:
            self.weights = dict(DEFAULT_WEIGHTS)

    @property
    def evidence_rules(self) -> EvidenceRules:
        return EvidenceRules(self.source_levels)


@runtime_checkable
class Grader(Protocol):
    name: str

    def grade(self, task: Task, output: AgentOutput, ctx: GradeContext) -> GradeResult:  # pragma: no cover
        ...


_REGISTRY: dict[str, type["BaseGrader"]] = {}


def register_grader(type_key: str, cls: type["BaseGrader"]) -> None:
    _REGISTRY[str(type_key)] = cls


def registered_graders() -> list[str]:
    if not _REGISTRY:
        _register_builtins()
    return sorted(_REGISTRY)


def get_grader(task: Task) -> "BaseGrader":
    """按 ``task.grader.type`` 返回 Grader 实例。"""
    key = (task.grader.type or "deterministic").strip()
    cls = _REGISTRY.get(key)
    if cls is None:
        # 懒注册：避免在 graders/*.py 被直接 import 时触发循环导入。
        _register_builtins()
        cls = _REGISTRY.get(key)
    if cls is None:
        raise GraderError(
            f"未知的 grader.type：{key}；已注册：{', '.join(registered_graders())}"
        )
    return cls(task.grader)


def resolve_weights(task: Task, base: Mapping[str, float] | None = None) -> dict[str, float]:
    """任务级权重覆盖全局权重（方案 4.2.3）。"""
    merged = dict(base or DEFAULT_WEIGHTS)
    if task.grader.weights:
        merged.update({k: float(v) for k, v in task.grader.weights.items()})
    return merged


def attach_evidence_levels(task: Task, output: AgentOutput, ctx: GradeContext) -> None:
    """评分前统一标注来源证据等级（E0~E5），并回填 ``Source``。"""
    year = None
    for req in task.expected.required_sources:
        if req.year:
            year = int(req.year)
            break
    judge_sources(output.sources, ctx.evidence_rules, task_year=year, annotate=True)


def run_checks(
    checks: Sequence[Check],
    task: Task,
    output: AgentOutput,
    ctx: GradeContext,
) -> list[CheckResult]:
    """批量执行声明式 check（顺序与 task.json 一致，保证确定性）。"""
    return [run_check(check, task, output, ctx) for check in checks]


class BaseGrader:
    """Grader 基类：统一收尾（错误分类、时间戳、序列化），子类只实现 :meth:`evaluate`。"""

    name: str = "base"
    version: str = "1.0"

    def __init__(self, spec: GraderSpec) -> None:
        self.spec = spec

    # -- 子类实现 ----------------------------------------------------------
    def evaluate(
        self, task: Task, output: AgentOutput, ctx: GradeContext
    ) -> tuple[list[CheckResult], str, str | None]:
        """返回 ``(checks, grader_mode, degraded_reason)``。"""
        raise NotImplementedError  # pragma: no cover

    def grader_versions(self, ctx: GradeContext) -> dict[str, Any]:
        return {"deterministic": self.version}

    # -- 统一入口 ----------------------------------------------------------
    def grade(self, task: Task, output: AgentOutput, ctx: GradeContext) -> GradeResult:
        attach_evidence_levels(task, output, ctx)
        checks, mode, reason = self.evaluate(task, output, ctx)
        from .scorer import finalize_grade

        return finalize_grade(
            task=task,
            output=output,
            ctx=ctx,
            checks=checks,
            grader_type=self.spec.type,
            grader_mode=mode,
            degraded_reason=reason,
            grader_versions=self.grader_versions(ctx),
        )


# 内置注册（模块末尾）
def _register_builtins() -> None:
    from .graders.deterministic import DeterministicGrader
    from .graders.hybrid import HybridGrader
    from .graders.manual import ManualGrader
    from .graders.semantic import SemanticGrader

    for cls in (DeterministicGrader, SemanticGrader, HybridGrader, ManualGrader):
        _REGISTRY.setdefault(cls.name, cls)


# 注意：注册改为**懒执行**（get_grader 内触发），避免 `from ..graders.manual import ...`
# 这类直接导入 gradurs 子模块时触发 `grader.py` 尚在初始化中的循环导入。
# 若 grader 模块已完整初始化（正常路径），这里仍尝试注册一次以尽早暴露错误。
try:  # pragma: no cover - 仅在无循环导入的常规路径下成功
    _register_builtins()
except ImportError:  # pragma: no cover
    pass


def _unused(*_: Any) -> None:  # pragma: no cover - 保持导入稳定
    _ = (Metrics, ScoreBreakdown, GraderSpec)
