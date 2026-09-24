"""Runner 协议与注册表（方案 5.2 / B-06）。

★ **Runner 三大铁律**（写死在基类里，子类不得绕过）：
1. Runner **不得**替 Agent 补全任何内容。采集不到的字段一律 ``None`` +
   ``usage_source="none"``。
2. Runner 必须捕获一切异常并转成 ``ErrorItem``；除 ``fail_fast`` 外不得让 CLI 崩溃。
3. ``timed_out=True`` 时 ``final_answer`` 为**已采集到的部分输出**（可为空串），
   ``task_success`` 由 Grader 判 False。
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from ..utils.hashing import hostname_hash, sha256_text
from ..utils.text import get_by_path, safe_str, truncate
from ..utils.timex import utcnow_iso
from .errors import ErrorCode, RunnerExecutionError, RunnerNotFoundError
from .logger import REDACTED, truncate_tool_result
from .models import (
    AgentOutput,
    AgentSpec,
    Environment,
    ErrorItem,
    ParseOutcome,
    RunContext,
    Source,
    Task,
    ToolCall,
    Usage,
)

__all__ = [
    "AgentRunner",
    "BaseRunner",
    "RunContext",
    "AgentOutput",
    "get_runner",
    "register_runner",
    "registered_runners",
    "builtin_runner_types",
    "parse_stdout",
    "parse_json_payload",
    "parse_jsonl_payload",
    "build_tool_calls",
    "build_sources",
    "build_citations",
    "extract_usage",
    "estimate_usage_from_chars",
    "read_usage_file",
    "runner_environment",
]

#: 内置 Runner 类型的注册顺序（也是 ``--help`` 展示顺序）。
_BUILTIN_TYPES: tuple[str, ...] = ("echo", "mock", "command", "http")

_REGISTRY: dict[str, type["BaseRunner"]] = {}
_BUILTIN_KEYS: set[str] = set()


@runtime_checkable
class AgentRunner(Protocol):
    """Agent 适配层协议（方案 5.2）。"""

    name: str

    def run(self, task: Task, ctx: RunContext) -> AgentOutput:  # pragma: no cover - 协议
        ...


def register_runner(type_key: str, cls: type["BaseRunner"], *, force: bool = False) -> None:
    """注册 Runner 类型。内置类型不允许被覆盖（除非 ``force=True``）。"""
    key = str(type_key).strip()
    if not key:
        raise ValueError("Runner 类型名不能为空")
    if key in _BUILTIN_KEYS and not force:
        raise ValueError(f"内置 Runner 类型 '{key}' 不允许被覆盖（如需强制，请传 force=True）")
    _REGISTRY[key] = cls


def registered_runners() -> list[str]:
    if not _REGISTRY:
        _register_builtins()
    return sorted(_REGISTRY)


def builtin_runner_types() -> tuple[str, ...]:
    return _BUILTIN_TYPES


def get_runner(spec: AgentSpec) -> "BaseRunner":
    """按 ``spec.type`` 取 Runner 实例。

    支持 ``command`` / ``http`` / ``mock`` / ``echo`` 与 ``dotted.path.ClassName`` 自定义扩展。
    未知类型 → :class:`RunnerNotFoundError`（列出已注册项）。
    """
    type_key = (spec.type or "command").strip()
    cls = _REGISTRY.get(type_key)
    if cls is None:
        # 懒注册：避免 runners/*.py 被直接 import 时的循环导入。
        _register_builtins()
        cls = _REGISTRY.get(type_key)
    if cls is None and "." in type_key:
        cls = _load_external_runner(type_key)
    if cls is None:
        raise RunnerNotFoundError(
            f"未注册的 Runner 类型：{type_key}；已注册：{', '.join(registered_runners())}。"
            "自定义 Runner 可用 'module.path.ClassName' 形式，或调用 register_runner() 注册"
        )
    return cls(spec)


def _load_external_runner(dotted: str) -> type["BaseRunner"] | None:
    """按 ``module.path.ClassName`` 动态导入（第三方插件逃生舱）。"""
    module_name, _, class_name = dotted.rpartition(".")
    if not module_name or not class_name:
        return None
    try:
        import importlib

        module = importlib.import_module(module_name)
    except ImportError:
        return None
    cls = getattr(module, class_name, None)
    if cls is None or not isinstance(cls, type):
        return None
    return cls


# --------------------------------------------------------------------------- #
# 基类
# --------------------------------------------------------------------------- #
class BaseRunner:
    """所有 Runner 的基类：负责**统一兜底**，子类只实现 :meth:`execute`。"""

    type_key: str = "base"
    requires_task_dir: bool = False

    def __init__(self, spec: AgentSpec) -> None:
        self.spec = spec
        self.name = spec.name or spec.type or self.type_key
        self.fail_fast = False

    # -- 子类实现 ----------------------------------------------------------
    def execute(self, task: Task, ctx: RunContext) -> AgentOutput:  # pragma: no cover - 抽象
        raise NotImplementedError

    # -- 统一入口（铁律 2）-------------------------------------------------
    def run(self, task: Task, ctx: RunContext) -> AgentOutput:
        started = utcnow_iso()
        try:
            output = self.execute(task, ctx)
        except RunnerExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - 铁律 2：必须兜住一切异常
            if self.fail_fast:
                raise
            output = AgentOutput(
                run_id=ctx.run_id,
                agent=self.name,
                agent_version=self.spec.version,
                model=self.spec.model or "",
                task_id=task.task_id,
                started_at=started,
                ended_at=utcnow_iso(),
                duration_ms=0,
                final_answer="",
                usage=Usage.none(),
                environment=runner_environment(),
            )
            output.errors.append(
                ErrorItem(
                    code=ErrorCode.UNKNOWN.value,
                    stage="runner",
                    message=f"Runner 执行异常：{_safe_exception_message(exc)}",
                    task_id=task.task_id,
                    run_id=ctx.run_id,
                    at=utcnow_iso(),
                )
            )
            output.raw = {"runner_exception": type(exc).__name__}
            if self.fail_fast:
                raise
        # 兜底：确保关键字段非空（但不补内容 —— 只补身份/时间）
        output.run_id = output.run_id or ctx.run_id
        output.task_id = output.task_id or task.task_id
        output.agent = output.agent or self.name
        output.agent_version = output.agent_version or self.spec.version
        output.model = output.model or (self.spec.model or "")
        output.started_at = output.started_at or started
        output.ended_at = output.ended_at or utcnow_iso()
        if output.environment is None or not output.environment.python:
            output.environment = runner_environment()
        if output.usage is None:
            output.usage = Usage.none()
        return output

    # -- 公共助手 ----------------------------------------------------------
    def answer_files(self, ctx: RunContext) -> dict[str, str]:
        """产出文件映射：workspace 内 ``output/`` 与约定 answer 文件。"""
        result: dict[str, str] = {}
        out_dir = Path(ctx.workspace_dir) / "output"
        if out_dir.is_dir():
            for path in sorted(out_dir.rglob("*")):
                if path.is_file():
                    result[path.name] = str(path.relative_to(Path(ctx.workspace_dir)))
        answer_file = Path(ctx.answer_file) if ctx.answer_file else None
        if answer_file and answer_file.is_file():
            result.setdefault(answer_file.name, str(answer_file.relative_to(Path(ctx.workspace_dir))))
        return result


def _safe_exception_message(exc: BaseException) -> str:
    """异常 → 可读信息，**不含文件路径 / 堆栈 / SQL / 密钥**。"""
    text = safe_str(exc) or type(exc).__name__
    text = text.replace("\n", " ").strip()
    from .logger import redact

    redacted = redact(text)
    return redacted if isinstance(redacted, str) else text


def runner_environment(*, sandbox_python: str | None = None) -> Environment:
    """当前运行环境（主机名只落 hash，不落原文）。"""
    import platform
    import sys

    return Environment(
        python=sandbox_python or platform.python_version(),
        platform=sys.platform if not sandbox_python else platform.system().lower(),
        hostname_hash=hostname_hash(),
        docker=False,
    )


# --------------------------------------------------------------------------- #
# stdout 三级解析（json → jsonl → text，方案 B-08）
# --------------------------------------------------------------------------- #
def parse_stdout(text: str, spec: AgentSpec | None = None) -> ParseOutcome:
    """按 ``parse.format`` 解析 stdout；失败按 ``parse.fallbacks`` 回退。

    回退链末尾永远是 ``text``（整段 stdout 作为 final_answer），
    并把解析失败写进 ``ParseOutcome.errors``（上层转 ``PARSING_FAILURE``）。
    """
    parse_spec = spec.parse if spec else None
    primary = (parse_spec.format if parse_spec else "text") or "text"
    fallbacks = list(parse_spec.fallbacks) if parse_spec else []
    chain = [primary] + [f for f in fallbacks if f != primary]
    if "text" not in chain:
        chain.append("text")

    errors: list[str] = []
    for fmt in chain:
        if fmt == "json":
            outcome = parse_json_payload(text, parse_spec)
            if outcome is not None:
                return ParseOutcome(format_used="json", data=outcome, text=text)
            errors.append("JSON 解析失败")
        elif fmt == "jsonl":
            outcome = parse_jsonl_payload(text)
            if outcome is not None:
                return ParseOutcome(format_used="jsonl", data=outcome, text=text)
            errors.append("JSONL 解析失败")
        elif fmt == "text":
            return ParseOutcome(format_used="text", data=None, text=text, errors=errors)
        else:
            errors.append(f"未知的 parse.format：{fmt}")

    return ParseOutcome(format_used="text", data=None, text=text, errors=errors)


def parse_json_payload(text: str, parse_spec: Any | None = None) -> dict[str, Any] | None:
    """从 stdout 中提取第一个完整 JSON 对象。

    容忍前导日志行（逐行尝试解析，成功即返回）。
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    candidates: list[str] = []
    if stripped[0] in "[{":
        candidates.append(stripped)
    for line in stripped.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            candidates.append(line)
    # 兜底：截取首个 { 到末个 }
    first = stripped.find("{")
    last = stripped.rfind("}")
    if 0 <= first < last:
        candidates.append(stripped[first : last + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"items": data}
    return None


def parse_jsonl_payload(text: str) -> dict[str, Any] | None:
    """把 JSONL 解析为 ``{"events": [...]}``；无有效行返回 ``None``。"""
    events: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    if not events:
        return None
    merged: dict[str, Any] = {"events": events}
    # 常见约定：最后一条含 final_answer 的事件即终态
    for event in reversed(events):
        for key in ("final_answer", "answer", "output", "result"):
            if key in event and isinstance(event[key], str):
                merged.setdefault("final_answer", event[key])
                break
        if "final_answer" in merged:
            break
    return merged


def payload_field(data: Mapping[str, Any] | None, path: str | None, default: Any = None) -> Any:
    """按 ``parse.*_field``（JSONPath 子集）取值；字段未配置时返回 ``default``。"""
    if not data or not path:
        return default
    value = get_by_path(data, path, default)
    return value


# --------------------------------------------------------------------------- #
# 结构化字段构造
# --------------------------------------------------------------------------- #
def build_tool_calls(
    raw: Any,
    *,
    mapping: Mapping[str, str] | None = None,
    run_id: str = "",
    task_id: str = "",
    at: str = "",
) -> list[ToolCall]:
    """把 Agent 上报的 tool_calls 归一化为 :class:`ToolCall` 列表。

    ``mapping`` 支持字段名映射（``name`` / ``arguments`` / ``ok`` / ``result`` / ``error``）。
    无法识别的元素**跳过**并在 ``error`` 里标注（不伪造）。
    """
    if not isinstance(raw, list):
        return []
    mp = {str(k): str(v) for k, v in (mapping or {}).items()}

    def pick(item: Mapping[str, Any], canonical: str, *aliases: str) -> Any:
        mapped = mp.get(canonical)
        if mapped and mapped in item:
            return item[mapped]
        if canonical in item:
            return item[canonical]
        for alias in aliases:
            if alias in item:
                return item[alias]
        return None

    calls: list[ToolCall] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        name = safe_str(pick(item, "name", "tool", "tool_name")) or "unknown"
        args_raw = pick(item, "arguments", "args", "input", "parameters")
        if isinstance(args_raw, Mapping):
            arguments = {str(k): v for k, v in args_raw.items()}
        elif isinstance(args_raw, str):
            parsed = parse_json_payload(args_raw)
            arguments = parsed if parsed is not None else {"raw": args_raw}
        else:
            arguments = {}
        result_raw = pick(item, "result", "output", "observation", "response")
        result_text, digest, was_truncated = truncate_tool_result(result_raw)
        ok_raw = pick(item, "ok", "success", "status")
        if isinstance(ok_raw, bool):
            ok = ok_raw
        elif isinstance(ok_raw, str):
            ok = ok_raw.strip().lower() in ("ok", "true", "success", "done", "1")
        else:
            error_text = safe_str(pick(item, "error", "error_message"))
            ok = not error_text
        error_text = safe_str(pick(item, "error", "error_message")) or None
        calls.append(
            ToolCall(
                call_id=safe_str(pick(item, "call_id", "id", "index")) or str(index + 1),
                name=name,
                arguments=arguments,
                result=result_text,
                result_sha256=digest,
                truncated=was_truncated,
                ok=ok,
                error=error_text,
                error_code=safe_str(pick(item, "error_code", "error_type")) or None,
                started_at=safe_str(pick(item, "started_at", "start_time")) or at or None,
                ended_at=safe_str(pick(item, "ended_at", "end_time")) or at or None,
                duration_ms=int(pick(item, "duration_ms", "latency_ms") or 0),
            )
        )
    return calls


def build_sources(raw: Any, *, task_id: str = "", at: str = "") -> list[Source]:
    """把 Agent 上报的 sources 归一化为 :class:`Source`（证据等级由 Grader 判定）。"""
    if not isinstance(raw, list):
        return []
    sources: list[Source] = []
    for item in raw:
        if isinstance(item, str):
            sources.append(Source(url=item, domain=_domain_of(item), fetched_at=at or None))
            continue
        if not isinstance(item, Mapping):
            continue
        url = safe_str(item.get("url") or item.get("link") or item.get("href")) or None
        sources.append(
            Source(
                url=url,
                title=safe_str(item.get("title") or item.get("name")) or None,
                domain=safe_str(item.get("domain")) or _domain_of(url),
                fetched_at=safe_str(item.get("fetched_at") or item.get("timestamp")) or at or None,
                content_sha256=safe_str(item.get("content_sha256") or item.get("sha256")) or None,
                snapshot_path=safe_str(item.get("snapshot_path")) or None,
                evidence_level=safe_str(item.get("evidence_level")) or "E0",
                evidence_reason=safe_str(item.get("evidence_reason")),
                year=_int_or_none(item.get("year")),
                is_official=bool(item.get("is_official", False)),
                clicked=item.get("clicked") if isinstance(item.get("clicked"), bool) else None,
            )
        )
    return sources


def build_citations(raw: Any) -> list["Any"]:
    """把 Agent 上报的 citations 归一化为 :class:`Citation` 列表（判定留给 Grader）。"""
    from .models import Citation

    if not isinstance(raw, list):
        return []
    out: list[Citation] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        out.append(
            Citation(
                citation_id=safe_str(item.get("citation_id") or item.get("id")) or f"cit{index + 1}",
                claim=safe_str(item.get("claim") or item.get("assertion")),
                source_ref=safe_str(item.get("source_ref") or item.get("source") or item.get("url")),
                locator=safe_str(item.get("locator") or item.get("page")) or None,
                supported=item.get("supported") if isinstance(item.get("supported"), bool) else None,
                unsupported_reason=safe_str(item.get("unsupported_reason")) or None,
                judge=safe_str(item.get("judge")) or "unjudged",
            )
        )
    return out


def _domain_of(url: str | None) -> str | None:
    if not url:
        return None
    text = url.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.split("@", 1)[-1].split(":", 1)[0]
    return text.lower() or None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Usage 三级回退（方案 4.7，★ 定死）
# --------------------------------------------------------------------------- #
def normalize_usage_payload(raw: Any) -> tuple[dict[str, Any] | None, bool]:
    """把 agent 上报的 usage 段归一化。

    返回 ``(usage_dict, estimated)``。``usage_dict`` 为 ``None`` 表示结构不可识别。
    支持别名：``input_tokens`` / ``output_tokens`` / ``prompt_tokens`` / ``total_cost`` …
    """
    if not isinstance(raw, Mapping):
        return None, False
    prompt = _int_or_none(
        raw.get("prompt_tokens", raw.get("input_tokens", raw.get("prompt")))
    )
    completion = _int_or_none(
        raw.get("completion_tokens", raw.get("output_tokens", raw.get("completion")))
    )
    total = _int_or_none(raw.get("total_tokens", raw.get("total")))
    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)
    cost = raw.get("cost_usd", raw.get("total_cost", raw.get("cost")))
    cost_value: float | None
    try:
        cost_value = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost_value = None
    if prompt is None and completion is None and total is None and cost_value is None:
        return None, False
    return (
        {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "cost_usd": cost_value,
        },
        False,
    )


def extract_usage(
    data: Mapping[str, Any] | None,
    field_path: str | None,
    *,
    raw_output: str = "",
    final_answer: str = "",
    usage_file: Path | None = None,
    pricing: Mapping[str, Any] | None = None,
) -> Usage:
    """三级回退采集 usage（方案 4.7）。

    1. ``agent_reported``：从 stdout JSON 的 usage 段读取。
    2. ``usage_file``：读 ``KAOYANBENCH_USAGE_FILE`` 指向的 JSON。
    3. ``estimated_from_chars``：``total_tokens ≈ utf8_len(stdout+final_answer)/4``，**estimated=True**。
    4. ``none``：都拿不到 → 全 ``None``，**绝不填 0**。
    """
    if data is not None and field_path:
        payload, _ = normalize_usage_payload(get_by_path(data, field_path))
        if payload is not None:
            return Usage(
                prompt_tokens=payload["prompt_tokens"],
                completion_tokens=payload["completion_tokens"],
                total_tokens=payload["total_tokens"],
                cost_usd=_cost_or_none(payload["cost_usd"], payload["total_tokens"], pricing),
                usage_source="agent_reported",
                estimated=False,
                pricing=dict(pricing) if pricing else None,
            )

    from_file = read_usage_file(usage_file)
    if from_file is not None:
        payload, _ = normalize_usage_payload(from_file)
        if payload is not None:
            return Usage(
                prompt_tokens=payload["prompt_tokens"],
                completion_tokens=payload["completion_tokens"],
                total_tokens=payload["total_tokens"],
                cost_usd=_cost_or_none(payload["cost_usd"], payload["total_tokens"], pricing),
                usage_source="usage_file",
                estimated=False,
                pricing=dict(pricing) if pricing else None,
            )

    text = (raw_output or "") + (final_answer or "")
    if text.strip():
        return estimate_usage_from_chars(text, pricing=pricing)

    return Usage.none()


def estimate_usage_from_chars(
    text: str, *, pricing: Mapping[str, Any] | None = None
) -> Usage:
    """字符估算：``total_tokens ≈ utf8_len(text) / 4``（方案 4.7 第 3 条）。"""
    raw = text or ""
    if not raw.strip():
        return Usage.none()
    tokens = max(1, int(len(raw.encode("utf-8")) / 4))
    return Usage(
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=tokens,
        cost_usd=None,
        usage_source="estimated_from_chars",
        estimated=True,
        pricing=dict(pricing) if pricing else None,
    )


def _cost_or_none(
    reported_cost: float | None,
    total_tokens: int | None,
    pricing: Mapping[str, Any] | None,
) -> float | None:
    """成本：优先用上报值；否则用单价表推算（``usage_source`` 仍由调用方决定）。"""
    if reported_cost is not None:
        return reported_cost
    return cost_from_pricing(total_tokens, pricing)


def cost_from_pricing(
    total_tokens: int | None, pricing: Mapping[str, Any] | None
) -> float | None:
    """按 ``{input_per_1m, output_per_1m}`` 单价表推算成本；缺任一必要信息返回 ``None``。"""
    if total_tokens is None or not pricing:
        return None
    price = pricing.get("input_per_1m", pricing.get("blended_per_1m"))
    try:
        per_1m = float(price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return round(total_tokens / 1_000_000 * per_1m, 6)


def read_usage_file(path: str | Path | None) -> dict[str, Any] | None:
    """读取 Agent 按约定写入的 usage JSON；不存在 / 非法 → ``None``。"""
    if not path:
        return None
    target = Path(path)
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def usage_missing(usage: Usage | None) -> bool:
    """是否属于「完全采集不到」（报告中 ``usage_missing_count`` 用）。"""
    return usage is None or usage.usage_source == "none"


def load_preset_answers(path: str | Path | None) -> dict[str, Any]:
    """读取 MockRunner 的预设答案文件（JSON）。"""
    if not path:
        return {}
    target = Path(path)
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def error_item(
    code: str,
    stage: str,
    message: str,
    *,
    task_id: str = "",
    run_id: str = "",
    tool_call_id: str | None = None,
    at: str | None = None,
) -> ErrorItem:
    """构造错误项的统一入口（自动脱敏 + 缺省时间）。

    注意：错误消息必须**不含**数据库语句 / 文件路径 / 密钥 / 完整手机号 —— 脱敏在这里兜底。
    """
    from .logger import redact

    text = safe_str(message)
    redacted = redact(text)
    # 去掉可能的绝对路径（只留文件名），避免把内部目录结构暴露给报告
    cleaned = _strip_paths(redacted if isinstance(redacted, str) else text)
    return ErrorItem(
        code=code,
        stage=stage,
        message=truncate(cleaned, 500)[0],
        task_id=task_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        at=at or utcnow_iso(),
    )


_PATH_RE = None


def _strip_paths(text: str) -> str:
    """把 ``/a/b/c/file.py`` 收敛为 ``file.py``（保留可读性，去掉内部结构）。"""
    global _PATH_RE
    if _PATH_RE is None:
        import re

        _PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:/[\w.\-]+){2,}")
    return _PATH_RE.sub(lambda m: m.group(0).rsplit("/", 1)[-1], text)


def tool_call_error_code(exc: BaseException) -> str:
    """异常 → 11 类错误码之一（ToolCall.error_code 用）。"""
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return ErrorCode.TIMEOUT.value
    if isinstance(exc, (FileNotFoundError, PermissionError, OSError)) or "tool" in name:
        return ErrorCode.TOOL_FAILURE.value
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)) or "parse" in name:
        return ErrorCode.PARSING_FAILURE.value
    return ErrorCode.UNKNOWN.value


# --------------------------------------------------------------------------- #
# 内置注册（模块末尾统一注册，避免 import 顺序问题）
# --------------------------------------------------------------------------- #
def _register_builtins() -> None:
    from .runners.command import CommandRunner
    from .runners.echo import EchoRunner
    from .runners.http import HttpRunner
    from .runners.mock import MockRunner

    for cls in (EchoRunner, MockRunner, CommandRunner, HttpRunner):
        _REGISTRY.setdefault(cls.type_key, cls)
        _BUILTIN_KEYS.add(cls.type_key)


# 注册改为懒执行（get_runner 内触发），避免直接 import runners 子模块时的循环导入；
# 常规路径下这里仍尝试注册一次，以便尽早暴露错误。
try:  # pragma: no cover
    _register_builtins()
except ImportError:  # pragma: no cover
    pass
