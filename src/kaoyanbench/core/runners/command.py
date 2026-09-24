"""CommandRunner：调用外部 CLI Agent（方案 B-08）。

能力：
- argv 模板渲染：``{prompt_file}`` / ``{workspace}`` / ``{answer_file}`` / ``{task_id}`` /
  ``{instruction}`` / ``{usage_file}`` / ``{references}`` / ``{output_dir}``
  （``{task_dir}`` 仅为向后兼容保留，解析到 workspace 隔离目录，不再暴露真实任务目录，
  对标 METR Task Standard 的环境/评分分离原则，防止 Agent 直读 task.json 作弊）
- ``stdin``：``none``（不喂）或 ``prompt``（把指令写到 stdin）
- ``cwd`` / ``env``（白名单 + 显式透传）
- 超时（SIGTERM → 宽限 → SIGKILL），``timed_out`` / ``exit_code`` 如实记录
- stdout 三级解析 json → jsonl → text；解析失败回落 text 并记 ``PARSING_FAILURE``
- usage 三级回退（agent_reported → usage_file → estimated_from_chars → none）
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...utils.text import get_by_path, safe_str
from ...utils.timex import utcnow_iso
from ..errors import ErrorCode
from ..models import AgentOutput, AgentSpec, ErrorItem, RunContext, Task, Usage
from ..runner import (
    BaseRunner,
    build_citations,
    build_sources,
    build_tool_calls,
    extract_usage,
    error_item,
    parse_stdout,
    runner_environment,
)
from ..sandbox import Sandbox

__all__ = ["CommandRunner", "render_template", "TEMPLATE_VARS"]

#: 内置模板变量（方案 5.2）。
#: ``task_dir`` 已废弃：为向后兼容保留键名，但值重定向到 workspace 隔离目录，
#: 不再暴露真实任务定义目录（含 task.json/expected 答案）。新配置请使用
#: ``{workspace}`` / ``{references}``（workspace 内的 fixture 副本）。
TEMPLATE_VARS: tuple[str, ...] = (
    "prompt_file",
    "workspace",
    "answer_file",
    "usage_file",
    "task_id",
    "instruction",
    "task_dir",
    "references",
    "output_dir",
)


def render_template(text: str, variables: Mapping[str, Any]) -> str:
    """安全模板渲染：只替换**已知变量**，未知名保持原样（便于尽早暴露配置错误）。

    故意不用 :meth:`str.format`，避免指令里的 ``{}`` 引发 ``KeyError``。
    """
    result = text
    for name in sorted(variables, key=len, reverse=True):
        value = variables[name]
        result = result.replace("{" + name + "}", str(value))
    return result


class CommandRunner(BaseRunner):
    type_key = "command"

    def __init__(self, spec: AgentSpec) -> None:
        super().__init__(spec)
        grace = spec.timeout_grace_sec
        self.sandbox = Sandbox(
            timeout_grace_sec=15 if grace is None else int(grace),
            env_passthrough=spec.env_passthrough,
        )

    # -- 渲染 --------------------------------------------------------------
    def template_vars(self, task: Task, ctx: RunContext) -> dict[str, Any]:
        workspace = Path(ctx.workspace_dir)
        # 防泄题（对标 METR/MAS评测隔离原则）：Agent 只能看到 workspace 隔离目录，
        # ``task_dir`` 重定向到 workspace，真实任务目录（含 task.json/expected）
        # 仅供内部 Grader/Engine 使用，绝不透给子进程。
        return {
            "prompt_file": str(workspace / "prompt.txt"),
            "workspace": str(workspace),
            "answer_file": str(ctx.answer_file) if ctx.answer_file else "",
            "usage_file": str(ctx.usage_file) if ctx.usage_file else "",
            "task_id": task.task_id,
            "instruction": task.instruction,
            "task_dir": str(workspace),
            "references": str(workspace / "references"),
            "output_dir": str(workspace / "output"),
        }

    def build_argv(self, task: Task, ctx: RunContext) -> list[str]:
        variables = self.template_vars(task, ctx)
        argv: list[str] = []
        raw_command: Sequence[str] = self.spec.command or []
        for token in raw_command:
            rendered = render_template(str(token), variables)
            argv.append(rendered)
        if not argv:
            raise ValueError("AgentSpec.command 为空，无法执行 command 型 Runner")
        return argv

    # -- 主流程 ------------------------------------------------------------
    def execute(self, task: Task, ctx: RunContext) -> AgentOutput:
        started = utcnow_iso()
        errors: list[ErrorItem] = []
        argv = self.build_argv(task, ctx)
        workspace = Path(ctx.workspace_dir)
        cwd = render_template(self.spec.cwd or "{workspace}", self.template_vars(task, ctx))

        env = self.sandbox.build_env(
            _as_workspace(workspace, ctx),
            extra=self.spec.env,
            replay_dir=ctx.snapshot_dir if ctx.offline_replay else None,
        )
        env.update(
            {
                "KAOYANBENCH_USAGE_FILE": str(ctx.usage_file) if ctx.usage_file else "",
                "KAOYANBENCH_ANSWER_FILE": str(ctx.answer_file) if ctx.answer_file else "",
                "KAOYANBENCH_TASK_ID": task.task_id,
            }
        )

        stdin_data = task.instruction if self.spec.stdin == "prompt" else None
        result = self.sandbox.run(
            argv,
            cwd=cwd if Path(cwd).is_dir() else workspace,
            env=env,
            timeout_sec=float(ctx.time_limit),
            stdin_data=stdin_data,
        )

        ended = utcnow_iso()
        if result.spawn_error:
            errors.append(
                error_item(
                    ErrorCode.TOOL_FAILURE.value,
                    "sandbox",
                    result.spawn_error,
                    task_id=task.task_id,
                    run_id=ctx.run_id,
                    at=ended,
                )
            )

        stdout = result.stdout
        parse_spec = self.spec.parse
        outcome = parse_stdout(stdout, self.spec)
        if outcome.errors:
            errors.append(
                error_item(
                    ErrorCode.PARSING_FAILURE.value,
                    "parse",
                    "；".join(outcome.errors) + "，已回退为 text",
                    task_id=task.task_id,
                    run_id=ctx.run_id,
                    at=ended,
                )
            )

        data = outcome.data
        final_answer = ""
        if data is not None:
            final_answer = safe_str(
                get_by_path(data, parse_spec.answer_field) if parse_spec.answer_field else None
            )
        if not final_answer:
            final_answer = self._answer_from_stdout_or_file(outcome, ctx)

        tool_calls = build_tool_calls(
            get_by_path(data, parse_spec.toolcalls_field) if parse_spec.toolcalls_field else None,
            mapping=self.spec.tool_call_mapping,
            run_id=ctx.run_id,
            task_id=task.task_id,
            at=ended,
        )
        sources = build_sources(
            get_by_path(data, parse_spec.sources_field) if parse_spec.sources_field else None,
            task_id=task.task_id,
            at=ended,
        )
        citations = build_citations(
            get_by_path(data, parse_spec.citations_field) if parse_spec.citations_field else None
        )
        search_trace = None
        if parse_spec.search_trace_field:
            raw_trace = get_by_path(data, parse_spec.search_trace_field)
            if isinstance(raw_trace, Mapping):
                search_trace = dict(raw_trace)
                search_trace.setdefault("evaluated_at", ended)

        pricing = self.spec.params.get("pricing") if isinstance(self.spec.params, Mapping) else None
        usage = extract_usage(
            data,
            parse_spec.usage_field,
            raw_output=stdout,
            final_answer=final_answer,
            usage_file=Path(ctx.usage_file) if ctx.usage_file else None,
            pricing=pricing if isinstance(pricing, Mapping) else None,
        )

        if result.timed_out:
            errors.append(
                error_item(
                    ErrorCode.TIMEOUT.value,
                    "runner",
                    f"超过 time_limit={ctx.time_limit}s，已强制终止"
                    + (f"（{result.signal_name}）" if result.signal_name else ""),
                    task_id=task.task_id,
                    run_id=ctx.run_id,
                    at=ended,
                )
            )

        # 契约：timed_out=True 时 final_answer 只能是用已采集到的部分输出
        success = (
            None
            if result.spawn_error
            else (not result.timed_out and result.exit_code == 0)
        )

        output = AgentOutput(
            run_id=ctx.run_id,
            agent=self.name,
            agent_version=self.spec.version,
            model=self.spec.model or (self.spec.params.get("model") or ""),
            provider=self.spec.provider,
            task_id=task.task_id,
            started_at=started,
            ended_at=ended,
            duration_ms=result.duration_ms,
            final_answer=final_answer,
            answer_files=self.answer_files(ctx),
            tool_calls=tool_calls,
            sources=sources,
            citations=citations,
            search_trace=search_trace,
            errors=errors,
            usage=usage,
            timed_out=result.timed_out,
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=result.stderr,
            raw={
                "argv": argv,
                "parse_format": outcome.format_used,
                "spawn_error": result.spawn_error,
                "killed": result.killed,
                "signal": result.signal_name,
                "env_keys": result.env_keys,
            },
            environment=runner_environment(),
        )
        output.success = success
        return output

    def _answer_from_stdout_or_file(self, outcome: Any, ctx: RunContext) -> str:
        """final_answer 回退链：answer 文件 → 解析出的文本 → 原始 stdout。"""
        answer_file = Path(ctx.answer_file) if ctx.answer_file else None
        if answer_file and answer_file.is_file():
            try:
                content = answer_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                content = ""
            if content.strip():
                parsed = _try_parse_answer_file(content)
                return parsed if parsed else content
        if outcome.data is not None and isinstance(outcome.data.get("final_answer"), str):
            return outcome.data["final_answer"]
        return outcome.text or ""

    @staticmethod
    def quote(argv: Sequence[str]) -> str:
        """把 argv 转成可复制的命令行字符串（日志/错误信息展示用，非执行用）。"""
        return " ".join(shlex.quote(str(x)) for x in argv)


def _as_workspace(workspace: Path, ctx: RunContext) -> Any:
    """构造一个轻量 Workspace 视图，供 ``build_sandbox_env`` 使用（不重复建目录）。"""
    from ..workspace import Workspace

    return Workspace(
        run_id=ctx.run_id,
        root=workspace,
        task_dir=Path(ctx.task_dir),
        references_dir=workspace / "references",
        output_dir=workspace / "output",
        prompt_file=workspace / "prompt.txt",
        answer_file=Path(ctx.answer_file) if ctx.answer_file else workspace / "answer.json",
        usage_file=Path(ctx.usage_file) if ctx.usage_file else workspace / "usage.json",
    )


def _try_parse_answer_file(content: str) -> str:
    """answer 文件若是 JSON，返回其中 ``final_answer`` / ``answer`` 字段。"""
    import json

    stripped = content.strip()
    if not stripped.startswith("{"):
        return ""
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("final_answer", "answer", "output", "result"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""
