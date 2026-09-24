"""数据模型（方案第 4 章 + 5.7 报告契约的逐字段落地）。

约定（与方案 4 章一致，**不得违反**）：
- 时间字段一律 ISO 8601 带时区字符串。
- 比率字段一律 0.0~1.0 浮点；报告层再转百分比。
- 金额单位 USD，字段后缀 ``_usd``。
- 耗时字段名显式带单位（``latency_seconds`` / ``duration_ms``）。
- 「采集不到」一律 ``None``（JSON 里为 ``null``），并配 ``*_source`` 说明原因；**禁止用 0 冒充**。

所有 dataclass 均支持 ``to_dict()`` / ``from_dict()`` 且满足 ``from_dict(to_dict(x)) == x``。
``to_dict(keep_nulls=True)``（默认）**保留 None 键**，以满足 5.7 的「字段齐全无 null 缺失」
（指键必须存在；值为 null 表示未采集，与「缺键」语义不同）。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from .errors import ERROR_CODES, TaskValidationError

# --------------------------------------------------------------------------- #
# 枚举常量（方案 4.1.1 / 4.1 / 4.9 / 4.10）
# --------------------------------------------------------------------------- #
CATEGORIES: tuple[str, ...] = (
    "search",
    "university",
    "policy",
    "exam",
    "pdf",
    "planning",
    "research",
    "hallucination",
)

CATEGORY_LABELS: dict[str, str] = {
    "search": "搜索与信息检索",
    "university": "院校与招生信息",
    "policy": "政策与时效信息",
    "exam": "真题与数据分析",
    "pdf": "PDF/资料处理",
    "planning": "学习规划",
    "research": "多步骤研究",
    "hallucination": "抗幻觉/证据验证",
}

DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard", "expert")

DIFFICULTY_LABELS: dict[str, str] = {
    "easy": "Easy",
    "medium": "Medium",
    "hard": "Hard",
    "expert": "Expert",
}

NETWORKS: tuple[str, ...] = ("offline", "online", "hybrid")

SPLITS: tuple[str, ...] = ("public", "private")

#: 7 个评分维度（顺序固定，报告堆叠条按此顺序）。
DIMENSIONS: tuple[str, ...] = (
    "factuality",
    "source_quality",
    "citation",
    "completeness",
    "tool_execution",
    "task_completion",
    "efficiency",
)

DIMENSION_LABELS: dict[str, str] = {
    "factuality": "事实性",
    "source_quality": "来源质量",
    "citation": "引用质量",
    "completeness": "完整性",
    "tool_execution": "工具执行",
    "task_completion": "任务完成",
    "efficiency": "效率",
}

#: 默认 7 维权重（方案 4.9 的满分除以 100）。
DEFAULT_WEIGHTS: dict[str, float] = {
    "factuality": 0.30,
    "source_quality": 0.20,
    "citation": 0.15,
    "completeness": 0.15,
    "tool_execution": 0.10,
    "task_completion": 0.05,
    "efficiency": 0.05,
}

#: 维度满分（方案 4.9 表格）。
DIMENSION_MAX: dict[str, int] = {
    "factuality": 30,
    "source_quality": 20,
    "citation": 15,
    "completeness": 15,
    "tool_execution": 10,
    "task_completion": 5,
    "efficiency": 5,
}

EVIDENCE_LEVELS: tuple[str, ...] = ("E0", "E1", "E2", "E3", "E4", "E5")

GRADER_TYPES: tuple[str, ...] = ("deterministic", "semantic", "hybrid", "manual")

GRADER_MODES: tuple[str, ...] = ("full", "mixed", "degraded")

#: usage_source 合法取值（方案 4.7）。
USAGE_SOURCES: tuple[str, ...] = (
    "agent_reported",
    "usage_file",
    "estimated_from_chars",
    "pricing_table",
    "none",
)

ANSWER_FORMAT_KINDS: tuple[str, ...] = ("text", "json", "file", "hybrid")

#: 16 种声明式 check 类型（方案 5.3）。
CHECK_TYPES: tuple[str, ...] = (
    "point_hit",
    "numeric",
    "string_eq",
    "string_contains",
    "regex",
    "set_includes",
    "json_schema",
    "file_exists",
    "file_json_match",
    "year_tag",
    "source_domain",
    "source_level",
    "citation_coverage",
    "must_not_claim",
    "constraint",
    "evidence_level",
)

#: check → 默认维度（方案 5.3 表格）。
CHECK_DEFAULT_DIMENSION: dict[str, str] = {
    "point_hit": "factuality",
    "numeric": "factuality",
    "string_eq": "factuality",
    "string_contains": "completeness",
    "regex": "factuality",
    "set_includes": "completeness",
    "json_schema": "completeness",
    "file_exists": "task_completion",
    "file_json_match": "factuality",
    "year_tag": "source_quality",
    "source_domain": "source_quality",
    "source_level": "source_quality",
    "citation_coverage": "citation",
    "must_not_claim": "factuality",
    "constraint": "task_completion",
    "evidence_level": "source_quality",
}

TASK_ID_RE = re.compile(r"^([A-Z]+)-(\d{3})$")
CLAIM_ID_RE = re.compile(r"^[A-Za-z][\w.-]*$")


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def _drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


def _as_dict_factory(keep_nulls: bool) -> Callable[[Any], Any]:  # pragma: no cover - 便利
    return (lambda v: v) if keep_nulls else _drop_none


class Serializable:
    """提供 ``to_dict`` / ``from_dict`` 的 mixin。

    子类需实现 :meth:`_field_schema`：``{字段名: (类型标记, 子模型或 None)}``。
    类型标记：``str/int/float/bool/json/dict/list/model/list_of/enum``。
    """

    def to_dict(self, *, keep_nulls: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, (kind, sub) in self._field_schema().items():
            value = getattr(self, name)
            out[name] = _encode(value, kind, sub)
        if not keep_nulls:
            out = _drop_none(out)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, **overrides: Any) -> Any:
        data = dict(data or {})
        data.update(overrides)
        kwargs: dict[str, Any] = {}
        for name, (kind, sub) in cls._field_schema().items():  # type: ignore[attr-defined]
            if name in data:
                kwargs[name] = _decode(data[name], kind, sub)
        return cls(**kwargs)

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:  # pragma: no cover - 抽象
        raise NotImplementedError


def _encode(value: Any, kind: str, sub: Any) -> Any:
    if value is None:
        return None
    if kind == "model":
        return value.to_dict() if isinstance(value, Serializable) else value
    if kind == "list_of":
        if isinstance(sub, type) and issubclass(sub, Serializable):
            return [v.to_dict() if isinstance(v, Serializable) else v for v in value]
        return list(value)
    if kind == "list_model":
        return [v.to_dict() if isinstance(v, Serializable) else v for v in value]
    if kind == "enum":
        return value.value if hasattr(value, "value") else value
    return value


def _decode(value: Any, kind: str, sub: Any) -> Any:
    if value is None:
        return None
    if kind == "model":
        if isinstance(value, sub):
            return value
        return sub.from_dict(value)  # type: ignore[union-attr]
    if kind in ("list_of", "list_model"):
        items = list(value or [])
        if kind == "list_model" and isinstance(sub, type) and issubclass(sub, Serializable):
            return [v if isinstance(v, sub) else sub.from_dict(v) for v in items]
        return items
    return value


# --------------------------------------------------------------------------- #
# 4.1.2 AnswerFormat
# --------------------------------------------------------------------------- #
@dataclass
class AnswerFormat(Serializable):
    kind: str = "text"
    schema: dict[str, Any] | None = None
    path: str | None = None
    filename: str | None = None
    text: bool | None = None
    file: str | None = None

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "kind": ("str", None),
            "schema": ("json", None),
            "path": ("str", None),
            "filename": ("str", None),
            "text": ("bool", None),
            "file": ("str", None),
        }

    @property
    def needs_file(self) -> bool:
        return self.kind in ("file", "hybrid")

    @property
    def expected_filename(self) -> str | None:
        return self.filename or self.file


# --------------------------------------------------------------------------- #
# 4.2.2 Point / SourceRequirement
# --------------------------------------------------------------------------- #
@dataclass
class Point(Serializable):
    id: str
    any_of: list[str] = field(default_factory=list)
    all_of: list[str] = field(default_factory=list)
    weight: float = 1.0
    dimension: str = "factuality"
    critical: bool = False
    description: str | None = None

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "id": ("str", None),
            "any_of": ("list_of", str),
            "all_of": ("list_of", str),
            "weight": ("float", None),
            "dimension": ("str", None),
            "critical": ("bool", None),
            "description": ("str", None),
        }


@dataclass
class SourceRequirement(Serializable):
    level_min: str = "E0"
    min_count: int = 1
    domain_suffix: list[str] = field(default_factory=list)
    year: int | None = None
    dimension: str = "source_quality"
    critical: bool = False

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "level_min": ("str", None),
            "min_count": ("int", None),
            "domain_suffix": ("list_of", str),
            "year": ("int", None),
            "dimension": ("str", None),
            "critical": ("bool", None),
        }


# --------------------------------------------------------------------------- #
# 4.2.1 Expected
# --------------------------------------------------------------------------- #
@dataclass
class Expected(Serializable):
    must_find: list[Point] = field(default_factory=list)
    must_not_claim: list[str] = field(default_factory=list)
    required_sources: list[SourceRequirement] = field(default_factory=list)
    required_fields: list[str] = field(default_factory=list)
    ground_truth: dict[str, Any] | None = None

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "must_find": ("list_model", Point),
            "must_not_claim": ("list_of", str),
            "required_sources": ("list_model", SourceRequirement),
            "required_fields": ("list_of", str),
            "ground_truth": ("json", None),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, **overrides: Any) -> "Expected":
        data = dict(data or {})
        data.update(overrides)
        return cls(
            must_find=[Point.from_dict(p) if not isinstance(p, Point) else p
                       for p in (data.get("must_find") or [])],
            must_not_claim=list(data.get("must_not_claim") or []),
            required_sources=[
                SourceRequirement.from_dict(s) if not isinstance(s, SourceRequirement) else s
                for s in (data.get("required_sources") or [])
            ],
            required_fields=list(data.get("required_fields") or []),
            ground_truth=data.get("ground_truth"),
        )

    def is_empty(self) -> bool:
        return not (
            self.must_find
            or self.must_not_claim
            or self.required_sources
            or self.required_fields
            or self.ground_truth
        )


# --------------------------------------------------------------------------- #
# 4.2.3 GraderSpec / RubricCriterion / Check
# --------------------------------------------------------------------------- #
@dataclass
class RubricCriterion(Serializable):
    id: str
    dimension: str
    question: str
    weight: float = 1.0

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "id": ("str", None),
            "dimension": ("str", None),
            "question": ("str", None),
            "weight": ("float", None),
        }


@dataclass
class Rubric(Serializable):
    prompt_version: str = "v1"
    criteria: list[RubricCriterion] = field(default_factory=list)

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {"prompt_version": ("str", None), "criteria": ("list_model", RubricCriterion)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, **overrides: Any) -> "Rubric":
        data = dict(data or {})
        data.update(overrides)
        return cls(
            prompt_version=data.get("prompt_version") or "v1",
            criteria=[RubricCriterion.from_dict(c) if not isinstance(c, RubricCriterion) else c
                      for c in (data.get("criteria") or [])],
        )


@dataclass
class Check:
    """声明式检查项（方案 5.3 的 16 种类型）。

    ``params`` 保存该类型的专有参数（如 ``any_of`` / ``path`` / ``values`` …），
    这样 16 种类型可以共用同一个 dataclass 而无需为每种类型写一个类。
    """

    id: str
    type: str
    dimension: str | None = None
    weight: float = 1.0
    critical: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    description: str | None = None

    #: 已知的通用键，其余键全部归入 ``params``。
    _META_KEYS: tuple[str, ...] = (
        "id",
        "type",
        "dimension",
        "weight",
        "critical",
        "description",
    )

    def __post_init__(self) -> None:
        if not self.dimension:
            self.dimension = CHECK_DEFAULT_DIMENSION.get(self.type, "completeness")

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def to_dict(self, *, keep_nulls: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "dimension": self.dimension,
            "weight": self.weight,
            "critical": self.critical,
        }
        if self.description is not None or keep_nulls:
            out["description"] = self.description
        # params 展开到同一层，保持 task.json 的可读性
        for key, value in self.params.items():
            if value is None and not keep_nulls:
                continue
            out[key] = value
        if not keep_nulls:
            out = _drop_none(out)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Check":  # type: ignore[override]
        data = dict(data or {})
        check_id = str(data.pop("id", "") or "")
        check_type = str(data.pop("type", "") or "")
        # 规范形态：params 平铺在同一层（to_dict 的写法，task.json 可读性优先）。
        params = {k: v for k, v in data.items() if k not in cls._META_KEYS}
        # 兼容形态：少数作者会写成 {"params": {...}}，此时把内层合并进来，
        # 避免出现 params={'params': {...}} 的双层包裹（会让 run_check 取不到参数）。
        nested = params.pop("params", None)
        if isinstance(nested, Mapping):
            merged = dict(nested)
            merged.update(params)
            params = merged
        return cls(
            id=check_id,
            type=check_type,
            dimension=data.get("dimension"),
            weight=float(data.get("weight", 1.0)),
            critical=bool(data.get("critical", False)),
            params=params,
            description=data.get("description"),
        )


@dataclass
class GraderSpec(Serializable):
    type: str = "deterministic"
    pass_threshold: float = 60.0
    weights: dict[str, float] | None = None
    checks: list[Check] = field(default_factory=list)
    rubric: Rubric | None = None

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "type": ("str", None),
            "pass_threshold": ("float", None),
            "weights": ("json", None),
            "checks": ("list_model", Check),
            "rubric": ("model", Rubric),
        }

    def to_dict(self, *, keep_nulls: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": self.type,
            "pass_threshold": self.pass_threshold,
            "weights": dict(self.weights) if self.weights is not None else None,
            "checks": [c.to_dict(keep_nulls=keep_nulls) for c in self.checks],
            "rubric": self.rubric.to_dict(keep_nulls=keep_nulls) if self.rubric else None,
        }
        if not keep_nulls:
            out = _drop_none(out)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, **overrides: Any) -> "GraderSpec":
        data = dict(data or {})
        data.update(overrides)
        return cls(
            type=data.get("type") or "deterministic",
            pass_threshold=float(data.get("pass_threshold", 60.0)),
            weights=dict(data["weights"]) if data.get("weights") else None,
            checks=[Check.from_dict(c) if not isinstance(c, Check) else c
                    for c in (data.get("checks") or [])],
            rubric=Rubric.from_dict(data["rubric"]) if data.get("rubric") else None,
        )

    def effective_weights(self, default: Mapping[str, float] | None = None) -> dict[str, float]:
        base = dict(default or DEFAULT_WEIGHTS)
        if self.weights:
            base.update({k: float(v) for k, v in self.weights.items()})
        return base


# --------------------------------------------------------------------------- #
# 5.2 AgentSpec / RunContext / AgentOutput
# --------------------------------------------------------------------------- #
@dataclass
class ParseSpec:
    format: str = "text"
    answer_field: str | None = None
    toolcalls_field: str | None = None
    sources_field: str | None = None
    citations_field: str | None = None
    usage_field: str | None = None
    search_trace_field: str | None = None
    fallbacks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "answer_field": self.answer_field,
            "toolcalls_field": self.toolcalls_field,
            "sources_field": self.sources_field,
            "citations_field": self.citations_field,
            "usage_field": self.usage_field,
            "search_trace_field": self.search_trace_field,
            "fallbacks": list(self.fallbacks),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ParseSpec":
        data = dict(data or {})
        return cls(
            format=str(data.get("format", "text") or "text"),
            answer_field=data.get("answer_field"),
            toolcalls_field=data.get("toolcalls_field"),
            sources_field=data.get("sources_field"),
            citations_field=data.get("citations_field"),
            usage_field=data.get("usage_field"),
            search_trace_field=data.get("search_trace_field"),
            fallbacks=list(data.get("fallbacks") or []),
        )


@dataclass
class AgentSpec:
    name: str
    type: str = "command"
    version: str | None = None
    model: str | None = None
    provider: str | None = None
    command: list[str] = field(default_factory=list)
    base_url: str | None = None
    api_key_env: str | None = None
    stdin: str = "none"
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    env_passthrough: list[str] = field(default_factory=list)
    timeout_grace_sec: int | None = None
    parse: ParseSpec = field(default_factory=ParseSpec)
    tool_call_mapping: dict[str, str] = field(default_factory=dict)
    answers: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "version": self.version,
            "model": self.model,
            "provider": self.provider,
            "command": list(self.command),
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "stdin": self.stdin,
            "cwd": self.cwd,
            "env": dict(self.env),
            "env_passthrough": list(self.env_passthrough),
            "timeout_grace_sec": self.timeout_grace_sec,
            "parse": self.parse.to_dict(),
            "tool_call_mapping": dict(self.tool_call_mapping),
            "answers": copy.deepcopy(self.answers),
            "params": copy.deepcopy(self.params),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, *, source_path: str | None = None) -> "AgentSpec":
        data = dict(data or {})
        known = {
            "name", "type", "version", "model", "provider", "command", "base_url",
            "api_key_env", "stdin", "cwd", "env", "env_passthrough", "timeout_grace_sec",
            "parse", "tool_call_mapping", "answers", "params",
        }
        # ``params:`` 是**一等字段**：显式展开其内容，避免出现
        # ``spec.params == {"params": {...}}`` 的双层嵌套（BUG-2）。
        raw_params = data.get("params")
        params: dict[str, Any] = (
            {str(k): v for k, v in raw_params.items()} if isinstance(raw_params, Mapping) else {}
        )
        # 其余未知键继续并入 params，保持向后兼容（历史上扁平书写也能读到）。
        for k, v in data.items():
            if k not in known:
                params.setdefault(str(k), v)

        # ``answers``：既支持顶层 ``answers:`` 直接注入，也支持写在
        # ``params.answers`` 里；前者优先（显式一等字段）。
        answers: dict[str, Any] = {}
        if isinstance(params.get("answers"), Mapping):
            answers.update({str(k): v for k, v in params["answers"].items()})
            params.pop("answers", None)
        if isinstance(data.get("answers"), Mapping):
            answers.update({str(k): v for k, v in data["answers"].items()})

        return cls(
            name=str(data.get("name", "") or ""),
            type=str(data.get("type", "command") or "command"),
            version=data.get("version"),
            model=data.get("model"),
            provider=data.get("provider"),
            command=[str(x) for x in (data.get("command") or [])],
            base_url=data.get("base_url"),
            api_key_env=data.get("api_key_env"),
            stdin=str(data.get("stdin", "none") or "none"),
            cwd=data.get("cwd"),
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            env_passthrough=[str(x) for x in (data.get("env_passthrough") or [])],
            timeout_grace_sec=(
                int(data["timeout_grace_sec"]) if data.get("timeout_grace_sec") is not None else None
            ),
            parse=ParseSpec.from_dict(data.get("parse")),
            tool_call_mapping={str(k): str(v) for k, v in (data.get("tool_call_mapping") or {}).items()},
            answers=answers,
            params=params,
            source_path=source_path,
        )


@dataclass
class RunContext:
    run_id: str
    attempt: int
    seed: int | None
    task_dir: Any  # pathlib.Path
    workspace_dir: Any
    snapshot_dir: Any | None
    offline_replay: bool
    time_limit: int
    tool_limit: int
    env: dict[str, str]
    usage_file: Any | None
    answer_file: Any | None


@dataclass
class ParseOutcome:
    """stdout 解析结果（Runner 内部使用，也便于单测）。"""

    format_used: str
    data: dict[str, Any] | None = None
    text: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_used": self.format_used,
            "data": self.data,
            "text": self.text,
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------- #
# 4.4 ToolCall
# --------------------------------------------------------------------------- #
@dataclass
class ToolCall(Serializable):
    call_id: str = ""
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    result_sha256: str = ""
    truncated: bool = False
    ok: bool = True
    error: str | None = None
    error_code: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int = 0

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "call_id": ("str", None),
            "name": ("str", None),
            "arguments": ("json", None),
            "result": ("str", None),
            "result_sha256": ("str", None),
            "truncated": ("bool", None),
            "ok": ("bool", None),
            "error": ("str", None),
            "error_code": ("str", None),
            "started_at": ("str", None),
            "ended_at": ("str", None),
            "duration_ms": ("int", None),
        }


# --------------------------------------------------------------------------- #
# 4.5 Source / 4.6 Citation
# --------------------------------------------------------------------------- #
@dataclass
class Source(Serializable):
    url: str | None = None
    title: str | None = None
    domain: str | None = None
    fetched_at: str | None = None
    content_sha256: str | None = None
    snapshot_path: str | None = None
    evidence_level: str = "E0"
    evidence_reason: str = ""
    year: int | None = None
    is_official: bool = False
    clicked: bool | None = None

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "url": ("str", None),
            "title": ("str", None),
            "domain": ("str", None),
            "fetched_at": ("str", None),
            "content_sha256": ("str", None),
            "snapshot_path": ("str", None),
            "evidence_level": ("str", None),
            "evidence_reason": ("str", None),
            "year": ("int", None),
            "is_official": ("bool", None),
            "clicked": ("bool", None),
        }


@dataclass
class Citation(Serializable):
    citation_id: str = ""
    claim: str = ""
    source_ref: str = ""
    locator: str | None = None
    supported: bool | None = None
    unsupported_reason: str | None = None
    judge: str = "unjudged"

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "citation_id": ("str", None),
            "claim": ("str", None),
            "source_ref": ("str", None),
            "locator": ("str", None),
            "supported": ("bool", None),
            "unsupported_reason": ("str", None),
            "judge": ("str", None),
        }


# --------------------------------------------------------------------------- #
# 4.7 Usage
# --------------------------------------------------------------------------- #
@dataclass
class Usage(Serializable):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    usage_source: str = "none"
    estimated: bool = False
    pricing: dict[str, Any] | None = None

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "prompt_tokens": ("int", None),
            "completion_tokens": ("int", None),
            "total_tokens": ("int", None),
            "cost_usd": ("float", None),
            "usage_source": ("str", None),
            "estimated": ("bool", None),
            "pricing": ("json", None),
        }

    @classmethod
    def none(cls) -> "Usage":
        """采集不到时的**唯一**合法构造方式：全 null + source='none'。"""
        return cls()

    def to_dict(self) -> dict[str, Any]:  # type: ignore[override]
        out = super().to_dict(keep_nulls=True)
        return out


# --------------------------------------------------------------------------- #
# 4.10 ErrorItem
# --------------------------------------------------------------------------- #
@dataclass
class ErrorItem(Serializable):
    code: str = "UNKNOWN"
    stage: str = "grade"
    message: str = ""
    task_id: str = ""
    run_id: str = ""
    tool_call_id: str | None = None
    at: str = ""

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "code": ("str", None),
            "stage": ("str", None),
            "message": ("str", None),
            "task_id": ("str", None),
            "run_id": ("str", None),
            "tool_call_id": ("str", None),
            "at": ("str", None),
        }


@dataclass
class Environment(Serializable):
    python: str = ""
    platform: str = ""
    hostname_hash: str | None = None
    docker: bool = False

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "python": ("str", None),
            "platform": ("str", None),
            "hostname_hash": ("str", None),
            "docker": ("bool", None),
        }


# --------------------------------------------------------------------------- #
# 4.3 Run
# --------------------------------------------------------------------------- #
@dataclass
class Run(Serializable):
    run_id: str = ""
    suite_id: str = ""
    attempt: int = 1
    seed: int | None = None
    agent: str = ""
    agent_version: str | None = None
    model: str = ""
    model_version: str | None = None
    provider: str | None = None
    task_id: str = ""
    started_at: str = ""
    ended_at: str = ""
    duration_ms: int = 0
    tool_calls: int = 0
    tool_calls_detail: list[ToolCall] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    final_answer: str = ""
    answer_files: dict[str, str] = field(default_factory=dict)
    errors: list[ErrorItem] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    timed_out: bool = False
    exit_code: int | None = None
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    search_trace: dict[str, Any] | None = None
    success: bool | None = None
    environment: Environment = field(default_factory=Environment)
    offline_replay: bool = False
    grader_mode: str | None = None
    tag: str | None = None
    # v1.1 重构：记录实际生效的预算（优先级 CLI>任务>suite>全局），保证
    # grade --run-id 复评时与首评口径一致；老数据缺失时回退到任务声明值。
    time_limit: int | None = None
    tool_limit: int | None = None

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "run_id": ("str", None),
            "suite_id": ("str", None),
            "attempt": ("int", None),
            "seed": ("int", None),
            "agent": ("str", None),
            "agent_version": ("str", None),
            "model": ("str", None),
            "model_version": ("str", None),
            "provider": ("str", None),
            "task_id": ("str", None),
            "started_at": ("str", None),
            "ended_at": ("str", None),
            "duration_ms": ("int", None),
            "tool_calls": ("int", None),
            "tool_calls_detail": ("list_model", ToolCall),
            "sources": ("list_model", Source),
            "citations": ("list_model", Citation),
            "final_answer": ("str", None),
            "answer_files": ("json", None),
            "errors": ("list_model", ErrorItem),
            "usage": ("model", Usage),
            "timed_out": ("bool", None),
            "exit_code": ("int", None),
            "stdout_sha256": ("str", None),
            "stderr_sha256": ("str", None),
            "search_trace": ("json", None),
            "success": ("bool", None),
            "environment": ("model", Environment),
            "offline_replay": ("bool", None),
            "grader_mode": ("str", None),
            "tag": ("str", None),
            "time_limit": ("int", None),
            "tool_limit": ("int", None),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, **overrides: Any) -> "Run":
        data = dict(data or {})
        data.update(overrides)
        return cls(
            run_id=data.get("run_id", "") or "",
            suite_id=data.get("suite_id", "") or "",
            attempt=int(data.get("attempt", 1) or 1),
            seed=data.get("seed"),
            agent=data.get("agent", "") or "",
            agent_version=data.get("agent_version"),
            model=data.get("model", "") or "",
            model_version=data.get("model_version"),
            provider=data.get("provider"),
            task_id=data.get("task_id", "") or "",
            started_at=data.get("started_at", "") or "",
            ended_at=data.get("ended_at", "") or "",
            duration_ms=int(data.get("duration_ms", 0) or 0),
            tool_calls=int(data.get("tool_calls", 0) or 0),
            tool_calls_detail=[ToolCall.from_dict(t) for t in (data.get("tool_calls_detail") or [])],
            sources=[Source.from_dict(s) for s in (data.get("sources") or [])],
            citations=[Citation.from_dict(c) for c in (data.get("citations") or [])],
            final_answer=data.get("final_answer", "") or "",
            answer_files=dict(data.get("answer_files") or {}),
            errors=[ErrorItem.from_dict(e) for e in (data.get("errors") or [])],
            usage=Usage.from_dict(data.get("usage")),
            timed_out=bool(data.get("timed_out", False)),
            exit_code=data.get("exit_code"),
            stdout_sha256=data.get("stdout_sha256"),
            stderr_sha256=data.get("stderr_sha256"),
            search_trace=data.get("search_trace"),
            success=data.get("success"),
            environment=Environment.from_dict(data.get("environment")),
            offline_replay=bool(data.get("offline_replay", False)),
            grader_mode=data.get("grader_mode"),
            tag=data.get("tag"),
            time_limit=data.get("time_limit"),
            tool_limit=data.get("tool_limit"),
        )

    def to_dict(self, *, keep_nulls: bool = True, with_tool_details: bool = True) -> dict[str, Any]:
        """``with_tool_details=False`` 用于 SuiteResult 的精简版（方案 4.9）。"""
        out = super().to_dict(keep_nulls=keep_nulls)
        if not with_tool_details:
            out.pop("tool_calls_detail", None)
            out["tool_calls_detail_ref"] = None
        return out

    def to_suite_dict(self) -> dict[str, Any]:
        """SuiteResult 内嵌的精简版：不含工具详情（详情在 JSONL）。"""
        out = super().to_dict(keep_nulls=True)
        out.pop("tool_calls_detail", None)
        # 保留 sources/citations —— 报告 5.7 需要来源与引用聚合
        out.pop("stdout_path", None)
        out.pop("stderr_path", None)
        return out


@dataclass
class AgentOutput(Serializable):
    """Runner 的统一返回结构（方案 5.2）。"""

    run_id: str = ""
    agent: str = ""
    agent_version: str | None = None
    model: str = ""
    model_version: str | None = None
    provider: str | None = None
    task_id: str = ""
    started_at: str = ""
    ended_at: str = ""
    duration_ms: int = 0
    final_answer: str = ""
    answer_files: dict[str, str] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    search_trace: dict[str, Any] | None = None
    errors: list[ErrorItem] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    timed_out: bool = False
    exit_code: int | None = None
    success: bool | None = None
    stdout: str = ""
    stderr: str = ""
    raw: dict[str, Any] | None = None
    environment: Environment = field(default_factory=Environment)

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "run_id": ("str", None),
            "agent": ("str", None),
            "agent_version": ("str", None),
            "model": ("str", None),
            "model_version": ("str", None),
            "provider": ("str", None),
            "task_id": ("str", None),
            "started_at": ("str", None),
            "ended_at": ("str", None),
            "duration_ms": ("int", None),
            "final_answer": ("str", None),
            "answer_files": ("json", None),
            "tool_calls": ("list_model", ToolCall),
            "sources": ("list_model", Source),
            "citations": ("list_model", Citation),
            "search_trace": ("json", None),
            "errors": ("list_model", ErrorItem),
            "usage": ("model", Usage),
            "timed_out": ("bool", None),
            "exit_code": ("int", None),
            "success": ("bool", None),
            "stdout": ("str", None),
            "stderr": ("str", None),
            "raw": ("json", None),
            "environment": ("model", Environment),
        }


# --------------------------------------------------------------------------- #
# 4.1 Task
# --------------------------------------------------------------------------- #
@dataclass
class Task:
    task_id: str
    category: str
    difficulty: str
    instruction: str
    network: str = "offline"
    time_limit: int = 900
    tool_limit: int = 60
    expected: Expected = field(default_factory=Expected)
    grader: GraderSpec = field(default_factory=GraderSpec)
    split: str = "public"
    tags: list[str] = field(default_factory=list)
    references_dir: str = "references"
    requires_snapshot: bool = False
    answer_format: AnswerFormat = field(default_factory=AnswerFormat)
    version: str = "1.0"
    created_at: str | None = None
    title: str | None = None
    task_dir: str | None = None

    # -- 序列化 ------------------------------------------------------------
    def to_dict(self, *, keep_nulls: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "task_id": self.task_id,
            "category": self.category,
            "difficulty": self.difficulty,
            "title": self.title,
            "instruction": self.instruction,
            "network": self.network,
            "time_limit": self.time_limit,
            "tool_limit": self.tool_limit,
            "expected": self.expected.to_dict(keep_nulls=keep_nulls),
            "grader": self.grader.to_dict(keep_nulls=keep_nulls),
            "split": self.split,
            "tags": list(self.tags),
            "references_dir": self.references_dir,
            "requires_snapshot": self.requires_snapshot,
            "answer_format": self.answer_format.to_dict(keep_nulls=keep_nulls),
            "version": self.version,
            "created_at": self.created_at,
        }
        if not keep_nulls:
            out = _drop_none(out)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, **overrides: Any) -> "Task":
        data = dict(data or {})
        data.update(overrides)
        return cls(
            task_id=str(data.get("task_id", "") or ""),
            category=str(data.get("category", "") or ""),
            difficulty=str(data.get("difficulty", "") or ""),
            instruction=str(data.get("instruction", "") or ""),
            network=str(data.get("network", "offline") or "offline"),
            time_limit=int(data.get("time_limit", 900) or 900),
            tool_limit=int(data.get("tool_limit", 60) or 60),
            expected=Expected.from_dict(data.get("expected")),
            grader=GraderSpec.from_dict(data.get("grader")),
            split=str(data.get("split", "public") or "public"),
            tags=[str(t) for t in (data.get("tags") or [])],
            references_dir=str(data.get("references_dir", "references") or "references"),
            requires_snapshot=bool(data.get("requires_snapshot", False)),
            answer_format=AnswerFormat.from_dict(data.get("answer_format")),
            version=str(data.get("version", "1.0") or "1.0"),
            created_at=data.get("created_at"),
            title=data.get("title"),
            task_dir=data.get("task_dir"),
        )

    @property
    def expected_pass_threshold(self) -> float:
        return float(self.grader.pass_threshold or 60.0)


# --------------------------------------------------------------------------- #
# 4.8 Metrics
# --------------------------------------------------------------------------- #
@dataclass
class Metrics(Serializable):
    task_success: bool = False
    factuality: float | None = None
    source_precision: float | None = None
    search_recall: float | None = None
    citation_accuracy: float | None = None
    hallucination_rate: float = 0.0
    tool_success_rate: float | None = None
    tool_calls: int = 0
    tokens: int | None = None
    latency_seconds: float = 0.0
    cost_usd: float | None = None

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "task_success": ("bool", None),
            "factuality": ("float", None),
            "source_precision": ("float", None),
            "search_recall": ("float", None),
            "citation_accuracy": ("float", None),
            "hallucination_rate": ("float", None),
            "tool_success_rate": ("float", None),
            "tool_calls": ("int", None),
            "tokens": ("int", None),
            "latency_seconds": ("float", None),
            "cost_usd": ("float", None),
        }

    def to_dict(self) -> dict[str, Any]:  # type: ignore[override]
        return super().to_dict(keep_nulls=True)


# --------------------------------------------------------------------------- #
# 4.9 ScoreBreakdown / CheckResult / GradeResult
# --------------------------------------------------------------------------- #
@dataclass
class ScoreBreakdown(Serializable):
    factuality: float = 0.0
    source_quality: float = 0.0
    citation: float = 0.0
    completeness: float = 0.0
    tool_execution: float = 0.0
    task_completion: float = 0.0
    efficiency: float = 0.0
    total: float = 0.0
    reallocated: bool = False
    neutral_dimensions: list[str] = field(default_factory=list)
    effective_weights: dict[str, float] = field(default_factory=dict)

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "factuality": ("float", None),
            "source_quality": ("float", None),
            "citation": ("float", None),
            "completeness": ("float", None),
            "tool_execution": ("float", None),
            "task_completion": ("float", None),
            "efficiency": ("float", None),
            "total": ("float", None),
            "reallocated": ("bool", None),
            "neutral_dimensions": ("list_of", str),
            "effective_weights": ("json", None),
        }

    def dimension_max(self, dim: str) -> float:
        return float(DIMENSION_MAX.get(dim, 0))

    def get(self, dim: str) -> float:
        return float(getattr(self, dim, 0.0) or 0.0)


@dataclass
class CheckResult(Serializable):
    id: str = ""
    type: str = ""
    dimension: str = "factuality"
    passed: bool | None = None
    weight: float = 1.0
    critical: bool = False
    detail: str = ""
    judge: str = "deterministic"

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "id": ("str", None),
            "type": ("str", None),
            "dimension": ("str", None),
            "passed": ("bool", None),
            "weight": ("float", None),
            "critical": ("bool", None),
            "detail": ("str", None),
            "judge": ("str", None),
        }

    @property
    def undetermined(self) -> bool:
        return self.passed is None


@dataclass
class GradeResult(Serializable):
    task_id: str = ""
    run_id: str = ""
    grader_type: str = "deterministic"
    grader_mode: str = "full"
    degraded_reason: str | None = None
    score: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    metrics: Metrics = field(default_factory=Metrics)
    checks: list[CheckResult] = field(default_factory=list)
    citations_judged: int = 0
    citations_supported: int = 0
    citations_unjudged: int = 0
    hallucination_violations: int = 0
    errors: list[ErrorItem] = field(default_factory=list)
    graded_at: str = ""
    grader_versions: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def _field_schema(cls) -> dict[str, tuple[str, Any]]:
        return {
            "task_id": ("str", None),
            "run_id": ("str", None),
            "grader_type": ("str", None),
            "grader_mode": ("str", None),
            "degraded_reason": ("str", None),
            "score": ("model", ScoreBreakdown),
            "metrics": ("model", Metrics),
            "checks": ("list_model", CheckResult),
            "citations_judged": ("int", None),
            "citations_supported": ("int", None),
            "citations_unjudged": ("int", None),
            "hallucination_violations": ("int", None),
            "errors": ("list_model", ErrorItem),
            "graded_at": ("str", None),
            "grader_versions": ("json", None),
        }

    @property
    def score_total(self) -> float:
        return float(self.score.total)

    @property
    def total_score(self) -> float:
        """``score_total`` 的别名（CLI/报告友好）。"""
        return float(self.score.total)

    @property
    def task_success(self) -> bool:
        """是否达标：等价于 ``metrics.task_success``（便捷访问）。"""
        return bool(self.metrics.task_success)

    @property
    def error_codes(self) -> list[str]:
        """``errors`` 中每条 :class:`ErrorItem` 的错误码（去重后保持出现顺序）。

        真实字段是 ``errors: list[ErrorItem]``；对外（CLI / 报告）经常只想拿「有哪些
        错误码」，故提供本只读便捷属性，避免调用方散落 ``[e.code for e in ...]``。
        """
        codes: list[str] = []
        for e in self.errors or []:
            code = getattr(e, "code", None)
            if code and code not in codes:
                codes.append(str(code))
        return codes

    @property
    def is_degraded(self) -> bool:
        return self.grader_mode == "degraded"


@dataclass
class Aggregates:
    """方案 4.9 ``aggregates`` 字段的 Python 映射。"""

    n_tasks: int = 0
    task_success_rate: float | None = None
    failure_rate: float | None = None
    pass_at_1: float | None = None
    pass_at_3: float | None = None
    score_mean: float | None = None
    score_median: float | None = None
    score_p90: float | None = None
    factuality_mean: float | None = None
    source_precision_mean: float | None = None
    search_recall_mean: float | None = None
    citation_accuracy_mean: float | None = None
    hallucination_rate_mean: float | None = None
    tool_success_rate_mean: float | None = None
    tool_calls_mean: float | None = None
    tool_calls_p90: float | None = None
    tokens_mean: float | None = None
    tokens_p90: float | None = None
    latency_seconds_mean: float | None = None
    latency_seconds_median: float | None = None
    latency_seconds_p90: float | None = None
    cost_usd_mean: float | None = None
    cost_usd_total: float | None = None
    usage_missing_count: int = 0
    usage_estimated_ratio: float | None = None
    degraded_task_count: int = 0
    grader_mode: str = "full"
    by_category: list[dict[str, Any]] = field(default_factory=list)
    by_difficulty: list[dict[str, Any]] = field(default_factory=list)
    error_counts: list[dict[str, Any]] = field(default_factory=list)
    #: 各指标的样本数（null 不参与均值时用于说明），键为指标名。
    sample_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            out[f.name] = copy.deepcopy(getattr(self, f.name))
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "Aggregates":
        data = dict(data or {})
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name in data:
                kwargs[f.name] = data[f.name]
        return cls(**kwargs)


# --------------------------------------------------------------------------- #
# 5.1 Suite（manifest）
# --------------------------------------------------------------------------- #
@dataclass
class Suite:
    id: str
    split: str = "public"
    version: str = "1.0"
    description: str = ""
    task_ids: list[str] | str = "all"
    filters: dict[str, Any] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "split": self.split,
            "version": self.version,
            "description": self.description,
            "task_ids": self.task_ids,
            "filters": copy.deepcopy(self.filters),
            "defaults": copy.deepcopy(self.defaults),
        }


# --------------------------------------------------------------------------- #
# 4.9 SuiteResult
# --------------------------------------------------------------------------- #
@dataclass
class SuiteResult:
    schema_version: str = "1.0"
    suite_id: str = ""
    split: str = "public"
    task_count: int = 0
    agent: str = ""
    agent_version: str | None = None
    model: str = ""
    runs_per_task: int = 1
    seed: int | None = None
    tag: str = ""
    created_at: str = ""
    offline_replay: bool = False
    environment: Environment = field(default_factory=Environment)
    runs: list[Run] = field(default_factory=list)
    grades: list[GradeResult] = field(default_factory=list)
    aggregates: Aggregates = field(default_factory=Aggregates)
    score_breakdown_mean: dict[str, float] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, keep_nulls: bool = True) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "suite_id": self.suite_id,
            "split": self.split,
            "task_count": self.task_count,
            "agent": self.agent,
            "agent_version": self.agent_version,
            "model": self.model,
            "runs_per_task": self.runs_per_task,
            "seed": self.seed,
            "tag": self.tag,
            "created_at": self.created_at,
            "offline_replay": self.offline_replay,
            "environment": self.environment.to_dict(keep_nulls=keep_nulls),
            "runs": [r.to_suite_dict() for r in self.runs],
            "grades": [g.to_dict(keep_nulls=keep_nulls) for g in self.grades],
            "aggregates": self.aggregates.to_dict(),
            "score_breakdown_mean": dict(self.score_breakdown_mean),
            "source": copy.deepcopy(self.source),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "SuiteResult":
        data = dict(data or {})
        return cls(
            schema_version=str(data.get("schema_version", "1.0") or "1.0"),
            suite_id=str(data.get("suite_id", "") or ""),
            split=str(data.get("split", "public") or "public"),
            task_count=int(data.get("task_count", 0) or 0),
            agent=str(data.get("agent", "") or ""),
            agent_version=data.get("agent_version"),
            model=str(data.get("model", "") or ""),
            runs_per_task=int(data.get("runs_per_task", 1) or 1),
            seed=data.get("seed"),
            tag=str(data.get("tag", "") or ""),
            created_at=str(data.get("created_at", "") or ""),
            offline_replay=bool(data.get("offline_replay", False)),
            environment=Environment.from_dict(data.get("environment")),
            runs=[Run.from_dict(r) for r in (data.get("runs") or [])],
            grades=[GradeResult.from_dict(g) for g in (data.get("grades") or [])],
            aggregates=Aggregates.from_dict(data.get("aggregates")),
            score_breakdown_mean={
                str(k): float(v)
                for k, v in (data.get("score_breakdown_mean") or {}).items()
                if v is not None
            },
            source=dict(data.get("source") or {}),
        )

    def grades_by_task(self) -> dict[str, list[GradeResult]]:
        out: dict[str, list[GradeResult]] = {}
        for g in self.grades:
            out.setdefault(g.task_id, []).append(g)
        for key in out:
            out[key].sort(key=lambda g: g.run_id)
        return out


# --------------------------------------------------------------------------- #
# 4.12 RegressionReport
# --------------------------------------------------------------------------- #
@dataclass
class MetricDelta:
    metric: str
    baseline: float | None = None
    current: float | None = None
    delta_pp: float | None = None
    delta_abs: float | None = None
    ratio: float | None = None
    direction: str = "flat"
    kind: str = "ratio"

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "baseline": self.baseline,
            "current": self.current,
            "delta_pp": self.delta_pp,
            "delta_abs": self.delta_abs,
            "ratio": self.ratio,
            "direction": self.direction,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MetricDelta":
        return cls(
            metric=str(data.get("metric", "")),
            baseline=data.get("baseline"),
            current=data.get("current"),
            delta_pp=data.get("delta_pp"),
            delta_abs=data.get("delta_abs"),
            ratio=data.get("ratio"),
            direction=str(data.get("direction", "flat")),
            kind=str(data.get("kind", "ratio")),
        )


@dataclass
class GateResult:
    name: str
    metric: str
    result: str = "skipped"  # pass | fail | warn | skipped
    delta_pp: float | None = None
    ratio: float | None = None
    threshold_delta_pp: float | None = None
    threshold_ratio: float | None = None
    level: str = "fail"
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "metric": self.metric,
            "result": self.result,
            "delta_pp": self.delta_pp,
            "ratio": self.ratio,
            "threshold_delta_pp": self.threshold_delta_pp,
            "threshold_ratio": self.threshold_ratio,
            "level": self.level,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GateResult":
        return cls(
            name=str(data.get("name", "")),
            metric=str(data.get("metric", "")),
            result=str(data.get("result", "skipped")),
            delta_pp=data.get("delta_pp"),
            ratio=data.get("ratio"),
            threshold_delta_pp=data.get("threshold_delta_pp"),
            threshold_ratio=data.get("threshold_ratio"),
            level=str(data.get("level", "fail")),
            message=str(data.get("message", "")),
        )


@dataclass
class RegressionReport:
    schema_version: str = "1.0"
    baseline_tag: str = ""
    current_tag: str = ""
    suite_id: str = ""
    compared_at: str = ""
    n_tasks_compared: int = 0
    n_tasks_missing_in_current: int = 0
    n_tasks_missing_in_baseline: int = 0
    deltas: list[MetricDelta] = field(default_factory=list)
    gates: list[GateResult] = field(default_factory=list)
    newly_failed_tasks: list[str] = field(default_factory=list)
    newly_passed_tasks: list[str] = field(default_factory=list)
    verdict: str = "pass"
    exit_code: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "baseline_tag": self.baseline_tag,
            "current_tag": self.current_tag,
            "suite_id": self.suite_id,
            "compared_at": self.compared_at,
            "n_tasks_compared": self.n_tasks_compared,
            "n_tasks_missing_in_current": self.n_tasks_missing_in_current,
            "n_tasks_missing_in_baseline": self.n_tasks_missing_in_baseline,
            "deltas": [d.to_dict() for d in self.deltas],
            "gates": [g.to_dict() for g in self.gates],
            "newly_failed_tasks": list(self.newly_failed_tasks),
            "newly_passed_tasks": list(self.newly_passed_tasks),
            "verdict": self.verdict,
            "exit_code": self.exit_code,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "RegressionReport":
        data = dict(data or {})
        return cls(
            schema_version=str(data.get("schema_version", "1.0")),
            baseline_tag=str(data.get("baseline_tag", "")),
            current_tag=str(data.get("current_tag", "")),
            suite_id=str(data.get("suite_id", "")),
            compared_at=str(data.get("compared_at", "")),
            n_tasks_compared=int(data.get("n_tasks_compared", 0) or 0),
            n_tasks_missing_in_current=int(data.get("n_tasks_missing_in_current", 0) or 0),
            n_tasks_missing_in_baseline=int(data.get("n_tasks_missing_in_baseline", 0) or 0),
            deltas=[MetricDelta.from_dict(d) for d in (data.get("deltas") or [])],
            gates=[GateResult.from_dict(g) for g in (data.get("gates") or [])],
            newly_failed_tasks=list(data.get("newly_failed_tasks") or []),
            newly_passed_tasks=list(data.get("newly_passed_tasks") or []),
            verdict=str(data.get("verdict", "pass")),
            exit_code=int(data.get("exit_code", 0) or 0),
        )


# --------------------------------------------------------------------------- #
# 4.11 Snapshot
# --------------------------------------------------------------------------- #
@dataclass
class SnapshotPage:
    url: str
    title: str = ""
    fetched_at: str = ""
    content_sha256: str = ""
    http_status: int | None = None
    snapshot_path: str = ""
    bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "fetched_at": self.fetched_at,
            "content_sha256": self.content_sha256,
            "http_status": self.http_status,
            "snapshot_path": self.snapshot_path,
            "bytes": self.bytes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SnapshotPage":
        return cls(
            url=str(data.get("url", "")),
            title=str(data.get("title", "") or ""),
            fetched_at=str(data.get("fetched_at", "") or ""),
            content_sha256=str(data.get("content_sha256", "") or ""),
            http_status=data.get("http_status"),
            snapshot_path=str(data.get("snapshot_path", "") or ""),
            bytes=int(data.get("bytes", 0) or 0),
        )


@dataclass
class SnapshotIndex:
    task_id: str
    captured_at: str = ""
    pages: list[SnapshotPage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "captured_at": self.captured_at,
            "pages": [p.to_dict() for p in self.pages],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "SnapshotIndex":
        data = dict(data or {})
        return cls(
            task_id=str(data.get("task_id", "") or ""),
            captured_at=str(data.get("captured_at", "") or ""),
            pages=[SnapshotPage.from_dict(p) for p in (data.get("pages") or [])],
        )


# --------------------------------------------------------------------------- #
# 5.1 ValidationIssue
# --------------------------------------------------------------------------- #
@dataclass
class ValidationIssue:
    level: str
    message: str
    task_id: str | None = None
    field: str | None = None
    file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "message": self.message,
            "task_id": self.task_id,
            "field": self.field,
            "file": self.file,
        }

    def __str__(self) -> str:  # pragma: no cover - 便利
        prefix = f"[{self.level}]"
        loc = " ".join(x for x in (self.task_id, self.field, self.file) if x)
        return f"{prefix} {loc}: {self.message}".strip()


# --------------------------------------------------------------------------- #
# 5.7 ReportModel（report.json 的 Python 映射）
# --------------------------------------------------------------------------- #
@dataclass
class ReportModel:
    """``report.json`` 的完整结构（方案 5.7 逐字段）。

    ``to_dict()`` 的键序即 5.7 给出的顺序；前端只依赖键名与语义，不依赖顺序。
    """

    schema_version: str = "1.0"
    generated_at: str = ""
    benchmark: dict[str, Any] = field(default_factory=dict)
    suite: dict[str, Any] = field(default_factory=dict)
    run: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    score_breakdown_mean: dict[str, Any] = field(default_factory=dict)
    by_category: list[dict[str, Any]] = field(default_factory=list)
    by_difficulty: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    comparison: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "benchmark": copy.deepcopy(self.benchmark),
            "suite": copy.deepcopy(self.suite),
            "run": copy.deepcopy(self.run),
            "summary": copy.deepcopy(self.summary),
            "score_breakdown_mean": copy.deepcopy(self.score_breakdown_mean),
            "by_category": copy.deepcopy(self.by_category),
            "by_difficulty": copy.deepcopy(self.by_difficulty),
            "errors": copy.deepcopy(self.errors),
            "tasks": copy.deepcopy(self.tasks),
            "comparison": copy.deepcopy(self.comparison),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ReportModel":
        data = dict(data or {})
        return cls(
            schema_version=str(data.get("schema_version", "1.0")),
            generated_at=str(data.get("generated_at", "")),
            benchmark=dict(data.get("benchmark") or {}),
            suite=dict(data.get("suite") or {}),
            run=dict(data.get("run") or {}),
            summary=dict(data.get("summary") or {}),
            score_breakdown_mean=dict(data.get("score_breakdown_mean") or {}),
            by_category=list(data.get("by_category") or []),
            by_difficulty=list(data.get("by_difficulty") or []),
            errors=list(data.get("errors") or []),
            tasks=list(data.get("tasks") or []),
            comparison=data.get("comparison"),
        )

    @property
    def grader_mode(self) -> str:
        return str(self.run.get("grader_mode", "full"))


# --------------------------------------------------------------------------- #
# 便捷构造
# --------------------------------------------------------------------------- #
def parse_gate_config(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """把 ``config/default.yaml`` 的 ``gates`` 段归一化为内部结构（供 regression 使用）。"""
    raw = dict(raw or {})
    mode = str(raw.pop("mode", "absolute") or "absolute")
    entries: dict[str, dict[str, Any]] = {}
    for name, value in raw.items():
        if not isinstance(value, Mapping):
            continue
        entries[str(name)] = {
            "max_delta_pp": value.get("max_delta_pp"),
            "max_ratio": value.get("max_ratio"),
            "level": str(value.get("level", "fail") or "fail"),
        }
    return {"mode": mode, "metrics": entries}


def run_is_failed(run: Run, grade: GradeResult | None) -> bool:
    """单次运行是否失败（报告用）：评分存在时以 task_success 为准，否则看 Runner 层。"""
    if grade is not None:
        return not bool(grade.metrics.task_success)
    return not bool(run.success)


def to_error_codes(errors: Iterable[ErrorItem]) -> list[str]:
    seen: list[str] = []
    for e in errors:
        if e.code in ERROR_CODES and e.code not in seen:
            seen.append(e.code)
    return seen


def empty_logical_value() -> None:
    """占位函数，保持 ``Any | None`` 风格的类型提示可读性（无运行时作用）。"""
    return None


__all__ = [
    "CATEGORIES",
    "CATEGORY_LABELS",
    "DIFFICULTIES",
    "DIFFICULTY_LABELS",
    "NETWORKS",
    "SPLITS",
    "DIMENSIONS",
    "DIMENSION_LABELS",
    "DEFAULT_WEIGHTS",
    "DIMENSION_MAX",
    "EVIDENCE_LEVELS",
    "GRADER_TYPES",
    "GRADER_MODES",
    "USAGE_SOURCES",
    "ANSWER_FORMAT_KINDS",
    "CHECK_TYPES",
    "CHECK_DEFAULT_DIMENSION",
    "TASK_ID_RE",
    "Serializable",
    "AnswerFormat",
    "Point",
    "SourceRequirement",
    "Expected",
    "RubricCriterion",
    "Rubric",
    "Check",
    "GraderSpec",
    "ParseSpec",
    "AgentSpec",
    "RunContext",
    "ParseOutcome",
    "ToolCall",
    "Source",
    "Citation",
    "Usage",
    "ErrorItem",
    "Environment",
    "Run",
    "AgentOutput",
    "Task",
    "Metrics",
    "ScoreBreakdown",
    "CheckResult",
    "GradeResult",
    "Aggregates",
    "Suite",
    "SuiteResult",
    "MetricDelta",
    "GateResult",
    "RegressionReport",
    "SnapshotPage",
    "SnapshotIndex",
    "ValidationIssue",
    "ReportModel",
    "parse_gate_config",
    "run_is_failed",
    "to_error_codes",
    "is_dataclass",
]
