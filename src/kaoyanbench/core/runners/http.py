"""HttpRunner：OpenAI 兼容端点 Runner（方案 B-09，P1）。

约束：
- **无 key 时不崩**：直接返回空答案 + ``usage_source="none"`` + 明确错误项。
- 超时 / 连接失败 → 转 ``ErrorItem``（``network_unreachable`` 语义），不抛裸异常。
- 只使用标准库 ``urllib.request``；不引 requests。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from ...utils.text import get_by_path, safe_str
from ...utils.timex import utcnow_iso
from ..errors import ErrorCode
from ..models import AgentOutput, RunContext, Task, Usage
from ..runner import (
    BaseRunner,
    build_citations,
    build_sources,
    build_tool_calls,
    error_item,
    normalize_usage_payload,
    runner_environment,
)

__all__ = ["HttpRunner"]

DEFAULT_TIMEOUT = 120


class HttpRunner(BaseRunner):
    type_key = "http"

    def execute(self, task: Task, ctx: RunContext) -> AgentOutput:
        started = utcnow_iso()
        errors: list[Any] = []
        base_url = (self.spec.base_url or "").rstrip("/")
        api_key_env = self.spec.api_key_env or ""
        import os

        api_key = os.environ.get(api_key_env) if api_key_env else None

        if not base_url or not api_key:
            reason = (
                "未配置 base_url（AgentSpec.base_url）"
                if not base_url
                else f"环境变量 {api_key_env} 为空，未发起请求"
            )
            errors.append(
                error_item(
                    ErrorCode.UNKNOWN.value,
                    "runner",
                    f"HTTP Runner 无法调用：{reason}",
                    task_id=task.task_id,
                    run_id=ctx.run_id,
                    at=started,
                )
            )
            return AgentOutput(
                run_id=ctx.run_id,
                agent=self.name,
                agent_version=self.spec.version,
                model=self.spec.model or "",
                provider="openai_compatible",
                task_id=task.task_id,
                started_at=started,
                ended_at=utcnow_iso(),
                duration_ms=0,
                final_answer="",
                answer_files={},
                tool_calls=[],
                sources=[],
                citations=[],
                search_trace=None,
                errors=errors,
                usage=Usage.none(),
                timed_out=False,
                exit_code=None,
                stdout="",
                stderr="",
                raw={"skipped": True, "reason": reason},
                environment=runner_environment(),
            )

        model = self.spec.model or str(self.spec.params.get("model") or "gpt-4o-mini")
        temperature = float(self.spec.params.get("temperature", 0) or 0)
        max_tokens = int(self.spec.params.get("max_tokens", 2048) or 2048)
        timeout = int(self.spec.params.get("timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
        timeout = min(timeout, max(1, ctx.time_limit))

        payload = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": task.instruction}],
        }
        url = base_url + ("" if base_url.endswith("/chat/completions") else "/chat/completions")
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )

        clock = time.monotonic()
        body: dict[str, Any] | None = None
        stderr = ""
        exit_code: int | None = None
        timed_out = False
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                raw = response.read().decode("utf-8", errors="replace")
                exit_code = int(getattr(response, "status", 200) or 200)
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                body = parsed
            else:
                stderr = "响应不是 JSON 对象"
        except urllib.error.HTTPError as exc:
            exit_code = int(exc.code)
            stderr = _http_error_text(exc)
            errors.append(
                error_item(
                    ErrorCode.TOOL_FAILURE.value,
                    "runner",
                    f"评测端点返回 HTTP {exc.code}",
                    task_id=task.task_id,
                    run_id=ctx.run_id,
                    at=utcnow_iso(),
                )
            )
        except TimeoutError:
            timed_out = True
            errors.append(
                error_item(
                    ErrorCode.TIMEOUT.value,
                    "runner",
                    f"HTTP 请求超时（{timeout}s）",
                    task_id=task.task_id,
                    run_id=ctx.run_id,
                    at=utcnow_iso(),
                )
            )
        except urllib.error.URLError as exc:
            errors.append(
                error_item(
                    ErrorCode.UNKNOWN.value,
                    "runner",
                    f"网络不可达：{_safe_reason(exc)}",
                    task_id=task.task_id,
                    run_id=ctx.run_id,
                    at=utcnow_iso(),
                )
            )
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            errors.append(
                error_item(
                    ErrorCode.PARSING_FAILURE.value,
                    "runner",
                    f"响应解析失败（{type(exc).__name__}）",
                    task_id=task.task_id,
                    run_id=ctx.run_id,
                    at=utcnow_iso(),
                )
            )

        duration_ms = int((time.monotonic() - clock) * 1000)
        final_answer = _extract_message_text(body)
        usage = _extract_http_usage(body)
        tool_calls = build_tool_calls(
            get_by_path(body, "$.choices[0].message.tool_calls") if body else None,
            run_id=ctx.run_id,
            task_id=task.task_id,
            at=utcnow_iso(),
        )
        sources = build_sources(
            get_by_path(body, "$.sources") if body else None, task_id=task.task_id
        )
        citations = build_citations(get_by_path(body, "$.citations") if body else None)

        output = AgentOutput(
            run_id=ctx.run_id,
            agent=self.name,
            agent_version=self.spec.version,
            model=model,
            provider="openai_compatible",
            task_id=task.task_id,
            started_at=started,
            ended_at=utcnow_iso(),
            duration_ms=duration_ms,
            final_answer=final_answer,
            answer_files=self.answer_files(ctx),
            tool_calls=tool_calls,
            sources=sources,
            citations=citations,
            search_trace=None,
            errors=errors,
            usage=usage,
            timed_out=timed_out,
            exit_code=exit_code,
            stdout=json.dumps(body, ensure_ascii=False) if body else "",
            stderr=stderr,
            raw={"url": url, "model": model} if body else None,
            environment=runner_environment(),
        )
        output.success = None if timed_out else exit_code == 200
        return output


def _extract_message_text(body: Mapping[str, Any] | None) -> str:
    if not body:
        return ""
    text = get_by_path(body, "$.choices[0].message.content")
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        # 多模态 content 数组
        parts = [
            safe_str(part.get("text"))
            for part in text
            if isinstance(part, Mapping) and part.get("type") in ("text", "output_text", None)
        ]
        return "".join(p for p in parts if p)
    return ""


def _extract_http_usage(body: Mapping[str, Any] | None) -> Usage:
    """``agent_reported`` 优先；无 usage 段时返回 ``none``（**不算字符估算**）。"""
    if not body:
        return Usage.none()
    payload, _ = normalize_usage_payload(body.get("usage"))
    if payload is None:
        return Usage.none()
    return Usage(
        prompt_tokens=payload["prompt_tokens"],
        completion_tokens=payload["completion_tokens"],
        total_tokens=payload["total_tokens"],
        cost_usd=payload["cost_usd"],
        usage_source="agent_reported",
        estimated=False,
    )


def _http_error_text(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - 极端
        body = ""
    from ..logger import redact

    text = redact(body[:500])
    return text if isinstance(text, str) else ""


def _safe_reason(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    from ..logger import redact

    text = safe_str(reason) or type(exc).__name__
    redacted = redact(text)
    return redacted if isinstance(redacted, str) else text
