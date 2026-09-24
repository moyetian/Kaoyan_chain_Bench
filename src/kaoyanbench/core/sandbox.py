"""Sandbox：进程级运行隔离（方案 2.3 / B-05）。

v1.0 隔离级别明确为**进程级**（README 不夸大）：
- 独立 workspace（由 :mod:`workspace` 负责目录）
- 环境变量白名单：**默认不透传任何 ``*_API_KEY``**，除 ``env_passthrough`` 显式声明
- HOME/TMPDIR/XDG_CACHE_HOME 重定向到 workspace（防缓存污染）
- 超时控制：SIGTERM → 宽限 ``timeout_grace_sec`` → SIGKILL
- Docker 模式：仅配置位占位（P2），未实现时显式报错而非静默降级
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import ConfigError
from .workspace import Workspace, workspace_env

__all__ = [
    "BASE_ENV_KEYS",
    "SENSITIVE_ENV_PATTERN",
    "SandboxResult",
    "Sandbox",
    "build_sandbox_env",
    "is_sensitive_env",
    "sensitive_env_warnings",
    "DockerNotAvailableError",
]

#: 基础环境变量白名单（方案 2.3）。
BASE_ENV_KEYS: tuple[str, ...] = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TERM",
    "PYTHONPATH",
    "SYSTEMROOT",  # Windows 需要
)

#: 敏感环境变量：**默认永不透传**，即使被写进 env_passthrough 也会警告。
SENSITIVE_ENV_PATTERN: tuple[str, ...] = (
    "API_KEY",
    "APIKEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "SESSION",
    "COOKIE",
    "OPENAI_KEY",
    "ANTHROPIC_KEY",
)


class DockerNotAvailableError(ConfigError):
    """请求 Docker 运行模式但 v1.0 未实现（P2 占位）。"""


def is_sensitive_env(name: str) -> bool:
    upper = (name or "").upper()
    return any(token in upper for token in SENSITIVE_ENV_PATTERN)


def sensitive_env_warnings(names: Iterable[str]) -> list[str]:
    """返回 ``env_passthrough`` 中敏感变量的告警文本（不抛异常）。

    显式声明即生效（不拦截），但必须让使用者知道密钥会被传给被测进程。
    """
    hits = sorted(
        {str(n).strip() for n in names if str(n).strip() and is_sensitive_env(str(n))}
    )
    if not hits:
        return []
    return [
        "env_passthrough 含敏感变量（"
        + ", ".join(hits)
        + "）：其值会原样传给被测进程；如非必要请从配置移除。"
    ]


def build_sandbox_env(
    workspace: Workspace,
    *,
    passthrough: Sequence[str] = (),
    extra: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    replay_dir: str | Path | None = None,
) -> dict[str, str]:
    """构造子进程环境：白名单基础变量 + workspace 重定向 + 显式透传。

    ★ 关键断言：``passthrough=[]`` 时返回值里**不含**任何 ``*_API_KEY``。
    """
    source = dict(os.environ if environ is None else environ)
    env: dict[str, str] = {}
    for key in BASE_ENV_KEYS:
        value = source.get(key)
        if value:
            env[key] = value
    if "PATH" not in env:
        # PATH 缺失会让子进程找不到命令；给一个保守默认值而不是空
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin"

    for message in sensitive_env_warnings(passthrough):
        warnings.warn(message, UserWarning, stacklevel=2)

    for name in passthrough:
        name = str(name).strip()
        if not name:
            continue
        value = source.get(name)
        if value is None or value == "":
            continue
        env[name] = value

    return workspace_env(
        workspace,
        base_env=env,
        extra={str(k): str(v) for k, v in (extra or {}).items()},
        replay_dir=replay_dir,
    )


@dataclass
class SandboxResult:
    """子进程执行结果（与方案 4.3 Run 的对应字段语义一致）。"""

    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    killed: bool = False
    signal_name: str | None = None
    spawn_error: str | None = None
    pid: int | None = None
    env_keys: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.spawn_error is None
            and not self.timed_out
            and self.exit_code == 0
        )


class Sandbox:
    """进程级沙箱执行器。"""

    def __init__(
        self,
        *,
        timeout_grace_sec: int = 15,
        env_passthrough: Sequence[str] = (),
        docker: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.timeout_grace_sec = max(0, int(timeout_grace_sec))
        self.env_passthrough = [str(x) for x in env_passthrough]
        self.docker = dict(docker or {})
        self.environ = environ
        if self.docker.get("enabled"):
            raise DockerNotAvailableError(
                "runtime.docker.enabled=true 但 v1.0 的 Docker 沙箱为 P2 占位项（方案 1.4/2.3）；"
                "请改用进程级沙箱，或参考 scripts/docker_run.sh 模板自行封装"
            )

    # -- 环境 --------------------------------------------------------------
    def build_env(
        self,
        workspace: Workspace,
        *,
        extra: Mapping[str, str] | None = None,
        replay_dir: str | Path | None = None,
    ) -> dict[str, str]:
        return build_sandbox_env(
            workspace,
            passthrough=self.env_passthrough,
            extra=extra,
            environ=self.environ,
            replay_dir=replay_dir,
        )

    # -- 执行 --------------------------------------------------------------
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        timeout_sec: float,
        stdin_data: str | None = None,
    ) -> SandboxResult:
        """执行命令并施加超时。**绝不抛未捕获异常**（除 ``fail_fast`` 由上层决定）。"""
        started = time.monotonic()
        cmd = [str(x) for x in argv]
        if not cmd:
            return SandboxResult(
                spawn_error="空命令（argv 为空）",
                duration_ms=0,
                env_keys=sorted(env.keys()),
            )

        popen_kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "env": dict(env),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            "start_new_session": True,  # 独立进程组，便于整组 kill
        }
        if os.name == "nt":  # pragma: no cover - 平台分支
            popen_kwargs.pop("start_new_session", None)
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)  # noqa: S603 - 命令来自配置，非用户输入拼接
        except FileNotFoundError:
            return SandboxResult(
                spawn_error=f"命令不存在：{Path(cmd[0]).name}",
                duration_ms=int((time.monotonic() - started) * 1000),
                env_keys=sorted(env.keys()),
            )
        except PermissionError:
            return SandboxResult(
                spawn_error=f"命令无执行权限：{Path(cmd[0]).name}",
                duration_ms=int((time.monotonic() - started) * 1000),
                env_keys=sorted(env.keys()),
            )
        except OSError as exc:
            return SandboxResult(
                spawn_error=f"无法启动子进程（{exc.strerror or type(exc).__name__}）",
                duration_ms=int((time.monotonic() - started) * 1000),
                env_keys=sorted(env.keys()),
            )

        timed_out = False
        killed = False
        signal_name: str | None = None
        try:
            stdout_bytes, stderr_bytes = proc.communicate(
                input=stdin_data.encode("utf-8") if stdin_data is not None else None,
                timeout=max(0.001, float(timeout_sec)),
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            signal_name = _terminate(proc, self.timeout_grace_sec)
            killed = signal_name in ("SIGKILL", "KILL")
            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - 极端
                stdout_bytes, stderr_bytes = b"", b""
        except (ValueError, OSError) as exc:  # pragma: no cover - 极端
            return SandboxResult(
                spawn_error=f"子进程通信失败（{type(exc).__name__}）",
                duration_ms=int((time.monotonic() - started) * 1000),
                env_keys=sorted(env.keys()),
            )

        duration = int((time.monotonic() - started) * 1000)
        return SandboxResult(
            exit_code=proc.returncode,
            stdout=_decode(stdout_bytes),
            stderr=_decode(stderr_bytes),
            duration_ms=duration,
            timed_out=timed_out,
            killed=killed,
            signal_name=signal_name,
            pid=proc.pid,
            env_keys=sorted(env.keys()),
        )


def _terminate(proc: "subprocess.Popen[bytes]", grace_sec: int) -> str | None:
    """SIGTERM → 宽限 → SIGKILL（Windows 用 terminate/kill）。返回被用到的信号名。"""
    if proc.poll() is not None:
        return None

    def _signal_group(sig: int) -> None:
        try:
            if os.name == "nt":  # pragma: no cover - 平台分支
                proc.terminate() if sig == signal.SIGTERM else proc.kill()
                return
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    _signal_group(signal.SIGTERM)
    deadline = time.monotonic() + max(0, grace_sec)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return "SIGTERM"
        time.sleep(0.05)
    if proc.poll() is not None:
        return "SIGTERM"
    _signal_group(signal.SIGKILL)
    for _ in range(20):
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    return "SIGKILL"


def _decode(raw: bytes) -> str:
    """容错解码：UTF-8 → 替换非法字节；失败退 latin-1（永不抛）。"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")
