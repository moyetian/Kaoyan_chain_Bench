"""MockRunner：离线预设答案 Runner（方案 B-07）。

行为（**离线跑通全链路的关键**）：
- 从 ``AgentSpec.answers``（``config/agents/mock.yaml`` 的 ``answers`` 段）取
  ``task_id`` 对应的预设答案；支持 ``"*"`` 通配默认答案。
- 预设答案可声明 ``final_answer`` / ``tool_calls`` / ``sources`` / ``citations`` /
  ``usage`` / ``files`` / ``search_trace``。
- 未命中预设 → 返回**明确的空答案 + UNKNOWN 错误项**，绝不编造内容。
- 支持 ``answers_file``（外部 JSON）以便任务集工程师补充。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ...utils.timex import utcnow_iso
from ..errors import ErrorCode
from ..models import AgentOutput, ErrorItem, RunContext, Task, Usage
from ..runner import (
    BaseRunner,
    build_citations,
    build_sources,
    build_tool_calls,
    extract_usage,
    load_preset_answers,
    normalize_usage_payload,
    runner_environment,
)

__all__ = ["MockRunner"]


class MockRunner(BaseRunner):
    """按预设答案输出，用于**离线**端到端验证。"""

    type_key = "mock"

    def __init__(self, spec: Any) -> None:
        super().__init__(spec)
        self._presets: dict[str, Any] = dict(spec.answers or {})
        external = spec.params.get("answers_file") if hasattr(spec, "params") else None
        if external:
            self._presets.update(load_preset_answers(external))

    # -- 预设查找 ----------------------------------------------------------
    def preset_for(self, task_id: str) -> Mapping[str, Any] | None:
        for key in (task_id, str(task_id).upper(), str(task_id).lower()):
            value = self._presets.get(key)
            if isinstance(value, Mapping):
                return value
        return None

    def default_preset(self) -> Mapping[str, Any] | None:
        for key in ("*", "default", "DEFAULT"):
            value = self._presets.get(key)
            if isinstance(value, Mapping):
                return value
        return None

    # -- 主流程 ------------------------------------------------------------
    def execute(self, task: Task, ctx: RunContext) -> AgentOutput:
        started = utcnow_iso()
        preset = self.preset_for(task.task_id) or self.default_preset()
        errors: list[ErrorItem] = []
        if preset is None:
            errors.append(
                ErrorItem(
                    code=ErrorCode.UNKNOWN.value,
                    stage="runner",
                    message=(
                        f"mock 未为 {task.task_id} 配置预设答案"
                        "（请在 config/agents/mock.yaml 的 answers 段补充，或提供 \"*\" 默认项）"
                    ),
                    task_id=task.task_id,
                    run_id=ctx.run_id,
                    at=utcnow_iso(),
                )
            )
            preset = {}

        final_answer = str(preset.get("final_answer", "") or preset.get("answer", "") or "")
        files = self._write_files(preset, ctx)
        answer_files = self.answer_files(ctx)

        tool_calls = build_tool_calls(
            preset.get("tool_calls"),
            run_id=ctx.run_id,
            task_id=task.task_id,
            at=started,
        )
        sources = build_sources(preset.get("sources"), task_id=task.task_id, at=started)
        citations = build_citations(preset.get("citations"))
        search_trace = preset.get("search_trace") if isinstance(preset.get("search_trace"), Mapping) else None
        if search_trace is not None:
            search_trace = dict(search_trace)
            search_trace.setdefault("evaluated_at", started)

        usage = self._usage(preset, final_answer)

        stdout = self._stdout_blob(preset, final_answer)
        return AgentOutput(
            run_id=ctx.run_id,
            agent=self.name,
            agent_version=self.spec.version or "1.0",
            model=self.spec.model or (self.spec.params.get("model") or "mock-model"),
            provider="mock",
            task_id=task.task_id,
            started_at=started,
            ended_at=utcnow_iso(),
            duration_ms=0,
            final_answer=final_answer,
            answer_files=answer_files,
            tool_calls=tool_calls,
            sources=sources,
            citations=citations,
            search_trace=search_trace,
            errors=errors,
            usage=usage,
            timed_out=False,
            exit_code=0 if preset else 1,
            stdout=stdout,
            stderr="",
            raw={"runner": "mock", "preset": dict(preset)},
            environment=runner_environment(),
        )

    # -- 文件产物 ----------------------------------------------------------
    def _write_files(self, preset: Mapping[str, Any], ctx: RunContext) -> list[str]:
        """把预设的 ``files`` 段写进 workspace，供 file 类 check 使用。"""
        written: list[str] = []
        files = preset.get("files")
        if not isinstance(files, Mapping):
            # 兼容 answer_format 要求单文件时的简写
            single = preset.get("file_content")
            filename = preset.get("filename")
            if single is not None and filename:
                files = {str(filename): single}
            else:
                return written
        workspace = Path(ctx.workspace_dir)
        for name, content in files.items():
            target = workspace / str(name)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, (dict, list)):
                    target.write_text(
                        json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True),
                        encoding="utf-8",
                    )
                else:
                    target.write_text(str(content), encoding="utf-8")
            except OSError:
                continue
            written.append(str(name))
        # 约定 answer 文件
        if ctx.answer_file:
            answer_file = Path(ctx.answer_file)
            if not answer_file.exists():
                try:
                    answer_file.write_text(
                        json.dumps(
                            {"final_answer": preset.get("final_answer", "")},
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
        return written

    # -- usage -------------------------------------------------------------
    def _usage(self, preset: Mapping[str, Any], final_answer: str) -> Usage:
        """mock 支持显式声明 usage_source，以便单测覆盖三级回退。"""
        declared = preset.get("usage")
        source = str(preset.get("usage_source") or "").strip()
        if declared is None and not source:
            return Usage.none()
        if declared is None:
            # 只声明了 source：走字符估算（estimated=True），或显式 none
            if source == "none":
                return Usage.none()
            from ..runner import estimate_usage_from_chars

            if source == "estimated_from_chars":
                return estimate_usage_from_chars(final_answer)
            return Usage(
                usage_source=source,
                estimated=source == "estimated_from_chars",
            )
        payload, _ = normalize_usage_payload(declared)
        if payload is None:
            return Usage.none()
        pricing = preset.get("pricing") if isinstance(preset.get("pricing"), Mapping) else None
        return Usage(
            prompt_tokens=payload["prompt_tokens"],
            completion_tokens=payload["completion_tokens"],
            total_tokens=payload["total_tokens"],
            cost_usd=payload["cost_usd"],
            usage_source=source or "agent_reported",
            estimated=source == "estimated_from_chars",
            pricing=dict(pricing) if pricing else None,
        )

    def _stdout_blob(self, preset: Mapping[str, Any], final_answer: str) -> str:
        """构造一段可被 :func:`parse_stdout` 解析的 stdout（含 usage 段）。"""
        if not preset:
            return ""
        blob: dict[str, Any] = {
            "final_answer": final_answer,
            "tool_calls": list(preset.get("tool_calls") or []),
            "sources": list(preset.get("sources") or []),
            "citations": list(preset.get("citations") or []),
        }
        if isinstance(preset.get("usage"), Mapping):
            blob["usage"] = dict(preset["usage"])
        if isinstance(preset.get("search_trace"), Mapping):
            blob["search_trace"] = dict(preset["search_trace"])
        return json.dumps(blob, ensure_ascii=False, indent=2, sort_keys=True)


# 兼容：让 extract_usage 也可用于 mock 的 stdout（任务集工程师按需调用）
_ = extract_usage
