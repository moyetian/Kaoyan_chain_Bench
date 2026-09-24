"""Workspace：每次运行独立的可写工作区（方案 2.3 / B-05）。

职责：
- 为每个 ``run_id`` 创建独立目录，**两次 run 的路径必然不同**。
- 把任务 ``references/`` 复制进 workspace（Agent 只能看到自己那一份，防串扰）。
- 提供 ``answer.json`` / ``usage.json`` / ``prompt.txt`` 的约定路径。
- 成功后清理、失败时保留（``--keep-workspace`` 强制保留）。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .errors import KaoyanBenchError
from .models import Task

__all__ = ["Workspace", "prepare_workspace", "cleanup_workspace", "WorkspaceError"]

#: 约定文件名的常量（Runner / Grader / 任务集三方共用，改动需同步文档）。
PROMPT_FILENAME = "prompt.txt"
ANSWER_FILENAME = "answer.json"
USAGE_FILENAME = "usage.json"
OUTPUT_DIRNAME = "output"
REFS_DIRNAME = "references"


class WorkspaceError(KaoyanBenchError):
    """workspace 准备/清理失败。"""


@dataclass
class Workspace:
    """一个 run 的工作区句柄。"""

    run_id: str
    root: Path
    task_dir: Path
    references_dir: Path
    output_dir: Path
    prompt_file: Path
    answer_file: Path
    usage_file: Path
    copied_references: list[str] = field(default_factory=list)
    created: bool = False

    def path(self) -> Path:
        return self.root

    def relative_files(self) -> dict[str, str]:
        """产出文件 → 相对 workspace 的路径（用于 ``Run.answer_files``）。"""
        result: dict[str, str] = {}
        if not self.output_dir.is_dir():
            return result
        for path in sorted(self.output_dir.rglob("*")):
            if path.is_file():
                result[path.name] = str(path.relative_to(self.root))
        if self.answer_file.is_file():
            result.setdefault(self.answer_file.name, str(self.answer_file.relative_to(self.root)))
        return result

    def read_answer_file(self) -> str | None:
        if self.answer_file.is_file():
            try:
                return self.answer_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None
        return None


def _safe_rmtree(path: Path) -> None:
    """安全删除：只删 workspace 根下的目录，且容忍部分失败。"""
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError:
        # 清理失败不应让整个评测崩掉（尤其 Windows 上文件占用）
        pass


def prepare_workspace(
    root: str | Path,
    run_id: str,
    task: Task,
    *,
    copy_references: bool = True,
    clean: bool = True,
) -> Workspace:
    """准备独立 workspace。

    :param root: workspace 根目录（通常 ``results/workspaces``）。
    :param run_id: 本 run 的唯一 id；作为子目录名。
    :param task: 任务；``task.task_dir/references`` 会被复制。
    :param clean: 目录已存在时先清空（幂等；run_id 唯一所以通常不存在）。
    """
    root_path = Path(root)
    workspace_root = root_path / run_id
    if clean and workspace_root.exists():
        _safe_rmtree(workspace_root)
    try:
        workspace_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceError(f"无法创建 workspace 目录（{exc.strerror or '未知错误'}）") from None

    task_dir = Path(task.task_dir) if task.task_dir else Path(".")
    refs_src = task_dir / task.references_dir
    refs_dst = workspace_root / REFS_DIRNAME
    output_dir = workspace_root / OUTPUT_DIRNAME
    output_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    if copy_references and refs_src.is_dir():
        refs_dst.mkdir(parents=True, exist_ok=True)
        for path in sorted(refs_src.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(refs_src)
            target = refs_dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(path, target)
            except OSError as exc:
                raise WorkspaceError(
                    f"复制 fixture 失败：{rel.name}（{exc.strerror or '未知错误'}）"
                ) from None
            copied.append(str(rel))

    prompt_file = workspace_root / PROMPT_FILENAME
    prompt_file.write_text(_build_prompt(task), encoding="utf-8")

    return Workspace(
        run_id=run_id,
        root=workspace_root,
        task_dir=task_dir,
        references_dir=refs_dst,
        output_dir=output_dir,
        prompt_file=prompt_file,
        answer_file=workspace_root / ANSWER_FILENAME,
        usage_file=workspace_root / USAGE_FILENAME,
        copied_references=copied,
        created=True,
    )


def _build_prompt(task: Task) -> str:
    """写入 workspace 的任务提示（含输出路径约定，便于 Agent 落盘）。"""
    lines = [
        f"# 任务 {task.task_id}",
        "",
        f"category: {task.category}",
        f"difficulty: {task.difficulty}",
        f"network: {task.network}",
        f"time_limit_seconds: {task.time_limit}",
        f"tool_limit: {task.tool_limit}",
        "",
        "## 指令",
        "",
        task.instruction,
        "",
        "## 输出约定",
        "",
        f"- fixture 目录：{REFS_DIRNAME}/",
        f"- 最终答案 JSON 建议写入：{ANSWER_FILENAME}",
        f"- usage 统计（如可上报）建议写入：{USAGE_FILENAME}",
        f"- 其它产物写入：{OUTPUT_DIRNAME}/",
        "",
        f"- 期望产物形态：{task.answer_format.kind}"
        + (f"（文件：{task.answer_format.expected_filename}）" if task.answer_format.needs_file else ""),
        "",
    ]
    return "\n".join(lines)


def cleanup_workspace(
    workspace: Workspace | str | Path,
    *,
    keep: bool = False,
    reason: str = "",
) -> bool:
    """按策略清理 workspace；返回是否实际删除。

    默认策略（方案 2.3）：**成功清理、失败保留**；``keep=True`` 一律保留。
    """
    root = workspace.root if isinstance(workspace, Workspace) else Path(workspace)
    if keep:
        return False
    if reason == "failure":
        # 失败保留现场，便于排查
        return False
    _safe_rmtree(root)
    return not root.exists()


def workspace_env(
    workspace: Workspace,
    *,
    base_env: dict[str, str] | None = None,
    extra: dict[str, str] | None = None,
    replay_dir: str | Path | None = None,
) -> dict[str, str]:
    """为子进程构造环境变量：HOME/TMPDIR/XDG 重定向进 workspace（方案 2.3）。

    ``KAOYANBENCH_REPLAY_DIR`` 是 L2 回放代理的预留环境变量语义（方案 2.11，P2）。
    """
    env = dict(base_env or {})
    home = workspace.root / "home"
    tmp = workspace.root / "tmp"
    cache = workspace.root / "cache"
    for d in (home, tmp, cache):
        d.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp)
    env["TEMP"] = str(tmp)
    env["TMP"] = str(tmp)
    env["XDG_CACHE_HOME"] = str(cache)
    env["KAOYANBENCH"] = "1"
    # 防泄题（v1.1 重构）：不再向子进程暴露真实任务目录。
    # 旧变量名保留以兼容历史 Agent 配置，但值重定向到 workspace 隔离目录；
    # 真实 ``workspace.task_dir`` 仅供内部 fixture 复制与 Grader 使用。
    # 对标 METR Task Standard：环境（workspace）与评分（task_dir）分离。
    env["KAOYANBENCH_TASK_DIR"] = str(workspace.root)
    env["KAOYANBENCH_WORKSPACE"] = str(workspace.root)
    env["KAOYANBENCH_ANSWER_FILE"] = str(workspace.answer_file)
    env["KAOYANBENCH_USAGE_FILE"] = str(workspace.usage_file)
    if replay_dir:
        env["KAOYANBENCH_REPLAY_DIR"] = str(replay_dir)
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def set_default_permissions(path: Path) -> None:
    """把 workspace 设为仅属主可读写（尽力而为；失败静默）。"""
    try:
        os.chmod(path, 0o700)
    except OSError:  # pragma: no cover - 平台差异
        pass


def list_workspaces(root: str | Path) -> list[str]:
    base = Path(root)
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def purge_workspaces(root: str | Path, keep_ids: Iterable[str] = ()) -> int:
    """清理 workspace 根下的全部目录（保留 ``keep_ids``），返回删除数量。"""
    base = Path(root)
    if not base.is_dir():
        return 0
    keep = set(keep_ids)
    removed = 0
    for path in sorted(base.iterdir(), key=lambda p: p.name):
        if not path.is_dir() or path.name in keep:
            continue
        _safe_rmtree(path)
        removed += 1
    return removed
