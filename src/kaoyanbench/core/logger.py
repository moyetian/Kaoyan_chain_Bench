"""RunLogger：JSONL 流式留痕（方案 2.4 / B-10）。

要求：
1. **边跑边写，崩溃不丢** —— 每条事件独立成行并立即 ``flush``；半截行不允许出现。
2. 超长 ``result`` **截断至 4000 字符**，同时保留未截断原文的 ``sha256``。
3. 敏感信息脱敏（api_key / token / password 等）→ ``***``。
4. 结构化事件类型（``run_start`` / ``tool_call`` / ``run_end`` / ``error``）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..utils.hashing import sha256_text
from ..utils.text import truncate
from .models import AgentOutput, ErrorItem, Run, ToolCall

__all__ = [
    "RESULT_TRUNCATE_LIMIT",
    "REDACTED",
    "EventType",
    "RunLogger",
    "redact",
    "redact_mapping",
    "truncate_tool_result",
    "read_jsonl",
    "iter_jsonl",
    "is_complete_jsonl",
]

#: 方案 2.4：超长 result 截断长度。
RESULT_TRUNCATE_LIMIT = 4000

REDACTED = "***"

#: 需要脱敏的键名模式（不区分大小写）。
_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret|password|passwd|credential|"
    r"authorization|bearer|private[_-]?key|session[_-]?id|cookie)",
    re.IGNORECASE,
)

#: 值层面的脱敏：``Bearer xxx`` / ``sk-xxxx`` / ``key=xxxx`` 等常见形态。
_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"), f"Bearer {REDACTED}"),
    (re.compile(r"\bsk-[A-Za-z0-9\-_]{8,}\b"), f"sk-{REDACTED}"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,})\b"), REDACTED),
    (
        re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password)\s*[:=]\s*([^\s,;\"'}]{4,})"),
        r"\1=" + REDACTED,
    ),
    # 中国大陆手机号：保留前 3 后 2（错误信息里不得出现完整手机号）
    (re.compile(r"\b(1[3-9]\d)\d{6}(\d{2})\b"), r"\1******\2"),
)


class EventType:
    """结构化事件类型（方案 2.4，用字符串常量而非枚举以保持 JSON 简洁）。"""

    RUN_START = "run_start"
    TOOL_CALL = "tool_call"
    RUN_END = "run_end"
    ERROR = "error"
    PROGRESS = "progress"

    ALL = (RUN_START, TOOL_CALL, RUN_END, ERROR, PROGRESS)


def redact(value: Any) -> Any:
    """递归脱敏：按键名匹配 + 按值正则替换。**纯函数**。"""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
                out[key] = REDACTED
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        text = value
        for pattern, repl in _VALUE_PATTERNS:
            text = pattern.sub(repl, text)
        return text
    return value


def redact_mapping(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """脱敏并保证返回 dict。"""
    result = redact(dict(data or {}))
    return result if isinstance(result, dict) else {}


def truncate_tool_result(result: Any) -> tuple[str, str, bool]:
    """返回 ``(截断文本, 原文 sha256, 是否截断)``。"""
    raw = "" if result is None else (result if isinstance(result, str) else str(result))
    digest = sha256_text(raw)
    text, was_truncated = truncate(raw, RESULT_TRUNCATE_LIMIT)
    return text, digest, was_truncated


class RunLogger:
    """单个 run 的 JSONL 日志写入器。

    ``results/runs/<run_id>.jsonl``：第 1 行为 ``run_start``，随后 ``tool_call`` 若干，
    以 ``run_end`` 收尾；异常时补 ``error`` 行。每行独立成 JSON，立即落盘。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str = "",
        truncate_limit: int = RESULT_TRUNCATE_LIMIT,
        redact_sensitive: bool = True,
    ) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.truncate_limit = int(truncate_limit)
        self.redact_sensitive = bool(redact_sensitive)
        self._count = 0
        self._closed = False

    # -- 写入 --------------------------------------------------------------
    def write_event(self, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        """写一条事件（单行 JSON，立即 flush）。"""
        if self._closed:
            return
        body: dict[str, Any] = {
            "event": event_type,
            "run_id": self.run_id,
            "seq": self._count,
        }
        data = dict(payload or {})
        if self.redact_sensitive:
            data = redact_mapping(data)
        body.update(data)
        self._append_line(body)

    def _append_line(self, body: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(body, ensure_ascii=False, default=_json_default)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
        self._count += 1

    def run_start(self, run: Run, task_meta: Mapping[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "suite_id": run.suite_id,
            "agent": run.agent,
            "agent_version": run.agent_version,
            "model": run.model,
            "task_id": run.task_id,
            "attempt": run.attempt,
            "seed": run.seed,
            "started_at": run.started_at,
            "tag": run.tag,
            "offline_replay": run.offline_replay,
            "environment": run.environment.to_dict(),
        }
        if task_meta:
            payload["task"] = dict(task_meta)
        self.write_event(EventType.RUN_START, payload)

    def tool_call(self, call: ToolCall, index: int | None = None) -> None:
        text, digest, was_truncated = truncate_tool_result(call.result)
        payload = {
            "call_id": call.call_id,
            "index": index,
            "name": call.name,
            "arguments": dict(call.arguments),
            "result": text[: self.truncate_limit],
            "result_sha256": call.result_sha256 or digest,
            "truncated": was_truncated or call.truncated,
            "ok": call.ok,
            "error": call.error,
            "error_code": call.error_code,
            "started_at": call.started_at,
            "ended_at": call.ended_at,
            "duration_ms": call.duration_ms,
        }
        self.write_event(EventType.TOOL_CALL, payload)

    def error(self, item: ErrorItem) -> None:
        self.write_event(EventType.ERROR, item.to_dict())

    def progress(self, message: str, **extra: Any) -> None:
        self.write_event(EventType.PROGRESS, {"message": message, **extra})

    def run_end(self, run: Run, *, stdout: str = "", stderr: str = "") -> None:
        payload = {
            "ended_at": run.ended_at,
            "duration_ms": run.duration_ms,
            "exit_code": run.exit_code,
            "timed_out": run.timed_out,
            "success": run.success,
            "tool_calls": run.tool_calls,
            "grader_mode": run.grader_mode,
            "final_answer_chars": len(run.final_answer or ""),
            "usage": run.usage.to_dict(),
            "stdout_sha256": run.stdout_sha256,
            "stderr_sha256": run.stderr_sha256,
            "stdout_tail": truncate(stdout, 1000)[0] if stdout else "",
            "stderr_tail": truncate(stderr, 1000)[0] if stderr else "",
        }
        self.write_event(EventType.RUN_END, payload)

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- 读取 --------------------------------------------------------------
    @property
    def event_count(self) -> int:
        return self._count

    def events(self) -> list[dict[str, Any]]:
        return read_jsonl(self.path)


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return str(obj)


# --------------------------------------------------------------------------- #
# 读取工具
# --------------------------------------------------------------------------- #
def iter_jsonl(path: str | Path) -> Iterable[dict[str, Any]]:
    """逐行读取 JSONL，**跳过损坏行**（崩溃时可能残留不完整行）。"""
    target = Path(path)
    if not target.is_file():
        return
    with target.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                yield item


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    return list(iter_jsonl(path))


def is_complete_jsonl(path: str | Path) -> bool:
    """判断 JSONL 是否「完整」：每行可解析，且含 ``run_end`` 事件。"""
    events = read_jsonl(path)
    if not events:
        return False
    target = Path(path)
    # 未闭合的半截行检测：读原始行数 vs 可解析行数
    try:
        raw_lines = [
            line for line in target.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
    except OSError:  # pragma: no cover
        return False
    if len(raw_lines) != len(events):
        return False
    return any(e.get("event") == EventType.RUN_END for e in events)


def write_agent_output_log(
    path: str | Path,
    output: AgentOutput,
    *,
    redact_sensitive: bool = True,
) -> Run:
    """把 :class:`AgentOutput` 落成 JSONL，便于无 Runner 的单测直接使用。"""
    run = run_from_output(output)
    logger = RunLogger(path, run_id=run.run_id, redact_sensitive=redact_sensitive)
    logger.run_start(run)
    for index, call in enumerate(output.tool_calls):
        logger.tool_call(call, index=index)
    for item in output.errors:
        logger.error(item)
    logger.run_end(run, stdout=output.stdout, stderr=output.stderr)
    logger.close()
    return run


def run_from_output(output: AgentOutput) -> Run:
    """``AgentOutput`` → ``Run``（补齐日志层字段；不做评分）。"""
    return Run(
        run_id=output.run_id,
        suite_id="",
        attempt=1,
        seed=None,
        agent=output.agent,
        agent_version=output.agent_version,
        model=output.model,
        model_version=output.model_version,
        provider=output.provider,
        task_id=output.task_id,
        started_at=output.started_at,
        ended_at=output.ended_at,
        duration_ms=output.duration_ms,
        tool_calls=len(output.tool_calls),
        tool_calls_detail=list(output.tool_calls),
        sources=list(output.sources),
        citations=list(output.citations),
        final_answer=output.final_answer,
        answer_files=dict(output.answer_files),
        errors=list(output.errors),
        usage=output.usage,
        timed_out=output.timed_out,
        exit_code=output.exit_code,
        stdout_sha256=sha256_text(output.stdout) if output.stdout else None,
        stderr_sha256=sha256_text(output.stderr) if output.stderr else None,
        search_trace=output.search_trace,
        success=None,
        environment=output.environment,
    )
