"""错误体系与规则化错误分类器（对应方案 4.10 / B-02）。

设计要点：
- 11 类 ErrorCode 是**字符串枚举**，直接序列化进 ``ErrorItem.code``。
- ``classify_errors`` 是**纯函数**：同输入必得同输出，不依赖时间 / 随机 / 字典序。
- 分类规则表由 :data:`RULES` 声明，便于单测逐条覆盖。
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from .models import AgentOutput, ErrorItem, GradeResult, Task


# --------------------------------------------------------------------------- #
# 11 类错误码（方案 4.10 固定枚举，顺序即枚举顺序，禁止随意调整）
# --------------------------------------------------------------------------- #
class ErrorCode(str, Enum):
    SEARCH_FAILURE = "SEARCH_FAILURE"
    SOURCE_SELECTION_FAILURE = "SOURCE_SELECTION_FAILURE"
    PARSING_FAILURE = "PARSING_FAILURE"
    TOOL_FAILURE = "TOOL_FAILURE"
    PLANNING_FAILURE = "PLANNING_FAILURE"
    CONTEXT_FAILURE = "CONTEXT_FAILURE"
    HALLUCINATION = "HALLUCINATION"
    CITATION_FAILURE = "CITATION_FAILURE"
    CALCULATION_FAILURE = "CALCULATION_FAILURE"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"

    def __str__(self) -> str:  # pragma: no cover - 便利方法
        return self.value


#: 方案 4.10 定义的 11 类错误，按枚举顺序排列。
ERROR_CODES: tuple[str, ...] = tuple(c.value for c in ErrorCode)

#: 报告层展示用中文名（``errors[]`` 与 markdown 表格共用）。
ERROR_LABELS: dict[str, str] = {
    ErrorCode.SEARCH_FAILURE.value: "检索失败",
    ErrorCode.SOURCE_SELECTION_FAILURE.value: "来源选择失败",
    ErrorCode.PARSING_FAILURE.value: "解析失败",
    ErrorCode.TOOL_FAILURE.value: "工具调用失败",
    ErrorCode.PLANNING_FAILURE.value: "规划失败",
    ErrorCode.CONTEXT_FAILURE.value: "上下文丢失",
    ErrorCode.HALLUCINATION.value: "幻觉",
    ErrorCode.CITATION_FAILURE.value: "引用失败",
    ErrorCode.CALCULATION_FAILURE.value: "计算错误",
    ErrorCode.TIMEOUT.value: "超时",
    ErrorCode.UNKNOWN.value: "未知错误",
}

#: ErrorItem.stage 合法值（方案 4.10）。
ERROR_STAGES: tuple[str, ...] = ("runner", "parse", "grade", "sandbox")

#: 错误分类时可选的「细化原因」，写进 ErrorItem.message 帮助定位。
REASON_LABELS: dict[str, str] = {
    "no_sources": "未采集到任何来源",
    "required_sources_unsatisfied": "required_sources 全部未满足",
    "low_source_precision": "来源中 E4/E5 占比过低",
    "answer_parse_failed": "期望 JSON/文件产物但解析失败",
    "tool_success_rate_low": "工具调用成功率过低",
    "planning_constraint_conflict": "规划约束冲突或阶段缺失",
    "answered_without_reading_material": "未使用给定资料即作答",
    "ground_truth_conflict": "与预期事实冲突",
    "citation_accuracy_low": "引用准确率过低或无引用",
    "calculation_mismatch": "数值校验未通过",
    "hard_timeout": "超过 time_limit 被强制终止",
}


# --------------------------------------------------------------------------- #
# 异常体系
# --------------------------------------------------------------------------- #
class KaoyanBenchError(Exception):
    """所有 KaoyanBench 异常基类。

    CLI 顶层只捕获本类（以及 unexpected 异常）并打印可读信息，
    **绝不把 traceback / 文件路径 / SQL / 密钥透出给报告**。
    """

    exit_code: int = 2

    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context or {})


class ConfigError(KaoyanBenchError):
    """配置加载 / 校验失败（含不支持的 YAML 语法）。"""


class TaskValidationError(KaoyanBenchError):
    """单个任务目录加载失败：带文件路径 + 字段路径 + 期望类型。"""

    def __init__(
        self,
        message: str,
        *,
        file_path: str | None = None,
        field_path: str | None = None,
        expected: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        pieces = [message]
        if file_path:
            pieces.append(f"file={file_path}")
        if field_path:
            pieces.append(f"field={field_path}")
        if expected:
            pieces.append(f"expected={expected}")
        super().__init__(" | ".join(pieces), context=context)
        self.file_path = file_path
        self.field_path = field_path
        self.expected = expected


class SuiteValidationError(KaoyanBenchError):
    """suite manifest 非法或引用了不存在的任务。"""


class RunnerNotFoundError(KaoyanBenchError):
    """未注册的 Runner 类型。"""


class RunnerExecutionError(KaoyanBenchError):
    """Runner 执行中出现不可恢复错误（fail_fast 时才向上抛）。"""


class GraderError(KaoyanBenchError):
    """Grader 配置或执行错误。"""


class ScoringError(KaoyanBenchError):
    """评分无法进行（例如全部维度 neutral）。"""


class RegressionError(KaoyanBenchError):
    """回归比对输入缺失或不可比。"""


class StoreError(KaoyanBenchError):
    """结果存储读写失败。"""


class SnapshotError(KaoyanBenchError):
    """快照读写 / 校验失败。"""


class ReporterNotFoundError(KaoyanBenchError):
    """请求的报告格式没有可用 Reporter 实现（例如 HTML 由前端负责）。"""


# --------------------------------------------------------------------------- #
# 分类结果容器
# --------------------------------------------------------------------------- #
class RawFinding:
    """分类器的输入：一个「待归类的事实」。

    分类器不直接构造 ErrorItem（那需要 task_id/run_id/时间），
    只产出 ``(code, reason, tool_call_id)`` 三元组，保持纯函数可单测。
    """

    __slots__ = ("code", "reason", "tool_call_id")

    def __init__(self, code: str, reason: str = "", tool_call_id: str | None = None) -> None:
        self.code = code
        self.reason = reason
        self.tool_call_id = tool_call_id

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "reason": self.reason, "tool_call_id": self.tool_call_id}

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RawFinding):
            return NotImplemented
        return (self.code, self.reason, self.tool_call_id) == (
            other.code,
            other.reason,
            other.tool_call_id,
        )

    def __repr__(self) -> str:  # pragma: no cover - 调试
        return f"RawFinding(code={self.code!r}, reason={self.reason!r})"


# --------------------------------------------------------------------------- #
# 分类规则（方案 4.10 表格，逐行落地）
# --------------------------------------------------------------------------- #
class ClassificationInput:
    """分类器所需的全部输入（全部为已归一化的原始值，无对象依赖）。"""

    __slots__ = (
        "task_id",
        "network",
        "category",
        "answer_format_kind",
        "has_sources",
        "required_sources",
        "required_sources_satisfied",
        "source_precision",
        "parse_failed",
        "tool_calls",
        "tool_success_rate",
        "planning_rules_present",
        "planning_rules_passed",
        "canary_missed",
        "hallucination_violations",
        "ground_truth_conflict",
        "citation_accuracy",
        "citation_count",
        "requires_citation",
        "numeric_check_failed",
        "timed_out",
    )

    def __init__(
        self,
        *,
        task_id: str = "",
        network: str = "offline",
        category: str = "",
        answer_format_kind: str = "text",
        has_sources: bool = False,
        required_sources: int = 0,
        required_sources_satisfied: int = 0,
        source_precision: float | None = None,
        parse_failed: bool = False,
        tool_calls: int = 0,
        tool_success_rate: float | None = None,
        planning_rules_present: bool = False,
        planning_rules_passed: bool = True,
        canary_missed: bool = False,
        hallucination_violations: int = 0,
        ground_truth_conflict: bool = False,
        citation_accuracy: float | None = None,
        citation_count: int = 0,
        requires_citation: bool = False,
        numeric_check_failed: bool = False,
        timed_out: bool = False,
    ) -> None:
        self.task_id = task_id
        self.network = network
        self.category = category
        self.answer_format_kind = answer_format_kind
        self.has_sources = has_sources
        self.required_sources = required_sources
        self.required_sources_satisfied = required_sources_satisfied
        self.source_precision = source_precision
        self.parse_failed = parse_failed
        self.tool_calls = tool_calls
        self.tool_success_rate = tool_success_rate
        self.planning_rules_present = planning_rules_present
        self.planning_rules_passed = planning_rules_passed
        self.canary_missed = canary_missed
        self.hallucination_violations = hallucination_violations
        self.ground_truth_conflict = ground_truth_conflict
        self.citation_accuracy = citation_accuracy
        self.citation_count = citation_count
        self.requires_citation = requires_citation
        self.numeric_check_failed = numeric_check_failed
        self.timed_out = timed_out


def _rule_timeout(src: ClassificationInput) -> str | None:
    return REASON_LABELS["hard_timeout"] if src.timed_out else None


def _rule_hallucination(src: ClassificationInput) -> str | None:
    if src.hallucination_violations > 0:
        return f"命中 must_not_claim {src.hallucination_violations} 次"
    if src.ground_truth_conflict:
        return REASON_LABELS["ground_truth_conflict"]
    return None


def _rule_search_failure(src: ClassificationInput) -> str | None:
    if src.network != "online":
        return None
    if not src.has_sources:
        return REASON_LABELS["no_sources"]
    if src.required_sources > 0 and src.required_sources_satisfied == 0:
        return REASON_LABELS["required_sources_unsatisfied"]
    return None


def _rule_source_selection(src: ClassificationInput) -> str | None:
    if not src.has_sources:
        return None
    if src.source_precision is not None and src.source_precision < 0.5:
        return REASON_LABELS["low_source_precision"]
    return None


def _rule_parsing(src: ClassificationInput) -> str | None:
    return REASON_LABELS["answer_parse_failed"] if src.parse_failed else None


def _rule_tool_failure(src: ClassificationInput) -> str | None:
    if src.tool_calls <= 0:
        return None
    if src.tool_success_rate is not None and src.tool_success_rate < 0.8:
        return REASON_LABELS["tool_success_rate_low"]
    return None


def _rule_planning(src: ClassificationInput) -> str | None:
    if src.category != "planning":
        return None
    if src.planning_rules_present and not src.planning_rules_passed:
        return REASON_LABELS["planning_constraint_conflict"]
    if not src.planning_rules_present:
        return "规划任务缺少阶段划分或约束声明"
    return None


def _rule_context(src: ClassificationInput) -> str | None:
    return REASON_LABELS["answered_without_reading_material"] if src.canary_missed else None


def _rule_citation(src: ClassificationInput) -> str | None:
    if src.citation_accuracy is not None and src.citation_accuracy < 0.5:
        return REASON_LABELS["citation_accuracy_low"]
    if src.requires_citation and src.citation_count == 0:
        return "required_fields 要求来源但未产出任何引用"
    return None


def _rule_calculation(src: ClassificationInput) -> str | None:
    return REASON_LABELS["calculation_mismatch"] if src.numeric_check_failed else None


#: 规则表：(错误码, 判定函数)。**顺序即优先级**，先命中者先产出。
#: 理由：TIMEOUT / HALLUCINATION 是「一票否决」级别，必须优先于其它派发原因。
RULES: tuple[tuple[str, Any], ...] = (
    (ErrorCode.TIMEOUT.value, _rule_timeout),
    (ErrorCode.HALLUCINATION.value, _rule_hallucination),
    (ErrorCode.SEARCH_FAILURE.value, _rule_search_failure),
    (ErrorCode.PARSING_FAILURE.value, _rule_parsing),
    (ErrorCode.PLANNING_FAILURE.value, _rule_planning),
    (ErrorCode.CONTEXT_FAILURE.value, _rule_context),
    (ErrorCode.CALCULATION_FAILURE.value, _rule_calculation),
    (ErrorCode.CITATION_FAILURE.value, _rule_citation),
    (ErrorCode.TOOL_FAILURE.value, _rule_tool_failure),
    (ErrorCode.SOURCE_SELECTION_FAILURE.value, _rule_source_selection),
)


def classify(src: ClassificationInput) -> list[RawFinding]:
    """按 :data:`RULES` 顺序产出全部命中的错误码（可能多条）。

    规则保证：**输入确定 → 输出确定**。无任何命中时返回 ``[]``，
    由调用方决定是否补 ``UNKNOWN``（见 :func:`classify_or_unknown`）。
    """
    findings: list[RawFinding] = []
    for code, fn in RULES:
        reason = fn(src)
        if reason:
            findings.append(RawFinding(code, reason))
    return findings


def classify_or_unknown(src: ClassificationInput) -> list[RawFinding]:
    """同 :func:`classify`，但在「无任何命中且本次运行确有异常迹象」时补 ``UNKNOWN``。

    「异常迹象」定义（写死，避免把正常任务误判为 UNKNOWN）：
    ``parse_failed`` 为真、或 ``tool_calls > 0 且 tool_success_rate is None``、
    或 ``timed_out`` 为真。其余情况返回 ``[]``（正常运行不应有错误码）。
    """
    findings = classify(src)
    if findings:
        return findings
    has_tool_anomaly = src.tool_calls > 0 and src.tool_success_rate is None
    if src.timed_out or src.parse_failed or has_tool_anomaly:
        return [RawFinding(ErrorCode.UNKNOWN.value, "无法归类的运行异常")]
    return []


def finding_to_error_item(
    finding: RawFinding,
    *,
    task_id: str,
    run_id: str,
    at: str,
    stage: str = "grade",
    tool_call_id: str | None = None,
) -> "ErrorItem":
    """把 :class:`RawFinding` 转成 :class:`~kaoyanbench.core.models.ErrorItem`。"""
    from .models import ErrorItem  # 局部导入，避免 models <-> errors 循环

    code = finding.code if finding.code in ERROR_CODES else ErrorCode.UNKNOWN.value
    return ErrorItem(
        code=code,
        stage=stage if stage in ERROR_STAGES else "grade",
        message=finding.reason or ERROR_LABELS.get(code, code),
        task_id=task_id,
        run_id=run_id,
        tool_call_id=tool_call_id or finding.tool_call_id,
        at=at,
    )


def error_counts(errors: Iterable[Any]) -> dict[str, int]:
    """统计错误码出现次数，**保证 11 类键全部存在**（缺失记 0，便于报告直接渲染）。"""
    counts: dict[str, int] = {code: 0 for code in ERROR_CODES}
    for item in errors:
        code = getattr(item, "code", None)
        if code is None and isinstance(item, dict):
            code = item.get("code")
        if code in counts:
            counts[code] += 1
        elif code:
            counts[ErrorCode.UNKNOWN.value] += 1
    return counts


def error_counts_ranked(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """按 count 降序输出 ``[{code, count, ratio}]``；count 相同按枚举序稳定排序。"""
    counts = error_counts(errors)
    total = sum(counts.values())
    order = {code: idx for idx, code in enumerate(ERROR_CODES)}
    items = [
        {
            "code": code,
            "count": count,
            "ratio": (count / total) if total else 0.0,
        }
        for code, count in counts.items()
    ]
    items.sort(key=lambda d: (-d["count"], order[d["code"]]))
    return items


__all__ = [
    "ErrorCode",
    "ERROR_CODES",
    "ERROR_LABELS",
    "ERROR_STAGES",
    "REASON_LABELS",
    "KaoyanBenchError",
    "ConfigError",
    "TaskValidationError",
    "SuiteValidationError",
    "RunnerNotFoundError",
    "RunnerExecutionError",
    "GraderError",
    "ScoringError",
    "RegressionError",
    "StoreError",
    "SnapshotError",
    "ReporterNotFoundError",
    "RawFinding",
    "ClassificationInput",
    "RULES",
    "classify",
    "classify_or_unknown",
    "finding_to_error_item",
    "error_counts",
    "error_counts_ranked",
]
