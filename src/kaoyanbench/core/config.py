"""配置加载 / 环境变量插值 / 校验（方案 B-04 + 5.11）。

零依赖：优先使用内置受限解析器 :mod:`kaoyanbench.utils.mini_yaml`；
若环境里恰好装了 pyyaml，则**仅在 mini_yaml 失败后**作为兜底（保持行为一致，
避免两套解析器产生歧义：mini_yaml 能解析的配置永远走 mini_yaml）。
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..utils import mini_yaml
from .errors import ConfigError
from .models import (
    DEFAULT_WEIGHTS,
    DIMENSIONS,
    GRADER_TYPES,
    AgentSpec,
    Suite,
)

__all__ = [
    "ENV_PATTERN",
    "ModelConfig",
    "RuntimeConfig",
    "EvaluationConfig",
    "BenchmarkConfig",
    "OutputConfig",
    "Config",
    "load_config",
    "interpolate",
    "find_project_root",
    "load_agent_spec",
    "load_source_levels",
    "load_semantic_config",
]

#: ``${ENV_VAR}`` 与 ``${ENV_VAR:-default}``。
ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_WEIGHT_TOL = 1e-6


# --------------------------------------------------------------------------- #
# 基础加载
# --------------------------------------------------------------------------- #
def read_yaml_file(path: Path) -> dict[str, Any]:
    """读取 YAML 文件；先走内置受限解析器，失败后再尝试 pyyaml。

    两条路径都失败时抛出**不带文件系统细节**的可读错误。
    """
    if not path.is_file():
        raise ConfigError(f"配置文件不存在：{path}")
    try:
        data = mini_yaml.load(path)
    except ConfigError as mini_exc:
        data = _try_pyyaml(path, mini_exc)
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"配置文件顶层必须是映射（key: value）：{path.name}")
    return dict(data)


def _try_pyyaml(path: Path, mini_exc: ConfigError) -> Any:
    try:
        import yaml  # type: ignore
    except ImportError:
        raise mini_exc from None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except Exception:  # pragma: no cover - 极端环境
        raise mini_exc from None


def interpolate(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """递归做 ``${VAR}`` 环境变量插值。

    - ``${VAR}``：变量不存在 → 空字符串（**不抛异常**，便于离线跑通）。
    - ``${VAR:-default}``：变量不存在或为空 → 使用 default。
    - 只处理字符串；数字/布尔原样返回。
    """
    environ = os.environ if env is None else env

    def _sub(text: str) -> str:
        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            default = match.group(2)
            current = environ.get(name)
            if current is None or current == "":
                return "" if default is None else default
            return current

        return ENV_PATTERN.sub(repl, text)

    if isinstance(value, str):
        return _sub(value)
    if isinstance(value, Mapping):
        return {k: interpolate(v, environ) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, environ) for v in value]
    return value


def find_project_root(start: str | Path | None = None) -> Path:
    """向上查找含 ``pyproject.toml`` 的项目根；找不到则返回 ``start`` 或 cwd。"""
    cur = Path(start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    env_root = os.environ.get("KAOYANBENCH_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return cur


# --------------------------------------------------------------------------- #
# 配置段
# --------------------------------------------------------------------------- #
@dataclass
class ModelConfig:
    provider: str = "openai_compatible"
    name: str = ""
    base_url: str = ""
    api_key_env: str = ""
    temperature: float = 0.0
    max_tokens: int = 2000
    timeout: int = 60
    max_retries: int = 1
    usage_file: bool = False
    pricing: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "name": self.name,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "usage_file": self.usage_file,
            "pricing": copy.deepcopy(self.pricing),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ModelConfig":
        data = dict(data or {})
        return cls(
            provider=str(data.get("provider", "openai_compatible") or "openai_compatible"),
            name=str(data.get("name", "") or ""),
            base_url=str(data.get("base_url", "") or ""),
            api_key_env=str(data.get("api_key_env", "") or ""),
            temperature=float(data.get("temperature", 0.0) or 0.0),
            max_tokens=int(data.get("max_tokens", 2000) or 2000),
            timeout=int(data.get("timeout", 60) or 60),
            max_retries=int(data.get("max_retries", 1) or 0),
            usage_file=bool(data.get("usage_file", False)),
            pricing=dict(data["pricing"]) if data.get("pricing") else None,
        )

    def api_key(self) -> str | None:
        """读取 api_key；未配置或为空 → ``None``（**不落盘**）。"""
        if not self.api_key_env:
            return None
        value = os.environ.get(self.api_key_env)
        return value or None


@dataclass
class RuntimeConfig:
    timeout: int = 900
    timeout_grace_sec: int = 15
    max_tool_calls: int = 100
    concurrency: int = 1
    workspace_root: str = "results/workspaces"
    keep_workspace: bool = False
    env_passthrough: list[str] = field(default_factory=list)
    fail_fast: bool = False
    docker: dict[str, Any] = field(default_factory=lambda: {"enabled": False, "image": ""})

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "RuntimeConfig":
        data = dict(data or {})
        return cls(
            timeout=int(data.get("timeout", 900) or 900),
            timeout_grace_sec=int(data.get("timeout_grace_sec", 15) or 0),
            max_tool_calls=int(data.get("max_tool_calls", 100) or 0),
            concurrency=int(data.get("concurrency", 1) or 1),
            workspace_root=str(data.get("workspace_root", "results/workspaces") or ""),
            keep_workspace=bool(data.get("keep_workspace", False)),
            env_passthrough=[str(x) for x in (data.get("env_passthrough") or [])],
            fail_fast=bool(data.get("fail_fast", False)),
            docker=dict(data.get("docker") or {"enabled": False, "image": ""}),
        )


@dataclass
class EvaluationConfig:
    runs: int = 1
    seed: int | None = 42
    offline_replay: bool = False
    snapshot_dir: str = "benchmark/snapshots"
    grader_type: str = "hybrid"
    pass_threshold: float = 60.0
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    strict_warn: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "EvaluationConfig":
        data = dict(data or {})
        weights = dict(DEFAULT_WEIGHTS)
        raw_weights = data.get("weights") or {}
        if raw_weights:
            for key, value in raw_weights.items():
                weights[str(key)] = float(value)
        return cls(
            runs=int(data.get("runs", 1) or 1),
            seed=data.get("seed"),
            offline_replay=bool(data.get("offline_replay", False)),
            snapshot_dir=str(data.get("snapshot_dir", "benchmark/snapshots") or ""),
            grader_type=str(data.get("grader_type", "hybrid") or "hybrid"),
            pass_threshold=float(data.get("pass_threshold", 60.0) or 60.0),
            weights=weights,
            strict_warn=bool(data.get("strict_warn", False)),
        )


@dataclass
class BenchmarkConfig:
    name: str = "KaoyanBench"
    version: str = "1.0"
    tasks_root: str = "benchmark/tasks"
    suites_root: str = "benchmark/suites"
    default_suite: str = "core50"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "BenchmarkConfig":
        data = dict(data or {})
        return cls(
            name=str(data.get("name", "KaoyanBench") or "KaoyanBench"),
            version=str(data.get("version", "1.0") or "1.0"),
            tasks_root=str(data.get("tasks_root", "benchmark/tasks") or ""),
            suites_root=str(data.get("suites_root", "benchmark/suites") or ""),
            default_suite=str(data.get("default_suite", "core50") or "core50"),
        )


@dataclass
class OutputConfig:
    dir: str = "reports"
    format: list[str] = field(default_factory=lambda: ["json", "markdown", "html"])
    results_dir: str = "results"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "OutputConfig":
        data = dict(data or {})
        return cls(
            dir=str(data.get("dir", "reports") or "reports"),
            format=[str(x) for x in (data.get("format") or ["json", "markdown", "html"])],
            results_dir=str(data.get("results_dir", "results") or "results"),
        )


# --------------------------------------------------------------------------- #
# 顶层 Config
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    root: Path
    path: Path | None = None
    benchmark: BenchmarkConfig = field(default_factory=BenchmarkConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    grader_model: ModelConfig = field(
        default_factory=lambda: ModelConfig(name="", provider="openai_compatible")
    )
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    evaluation: EvaluationConfig = field(default_factory=EvaluationConfig)
    gates: dict[str, Any] = field(default_factory=dict)
    output: OutputConfig = field(default_factory=OutputConfig)
    agent: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    source_levels_path: str = "config/graders/source_levels.yaml"
    semantic_path: str = "config/graders/semantic_v1.yaml"
    agents_dir: str = "config/agents"
    allow_same_model: bool = False

    # -- 路径解析（相对项目根）---------------------------------------------
    def path_of(self, relative: str) -> Path:
        if not relative:
            return self.root
        p = Path(relative)
        return p if p.is_absolute() else (self.root / p)

    @property
    def tasks_root(self) -> Path:
        return self.path_of(self.benchmark.tasks_root)

    @property
    def suites_root(self) -> Path:
        return self.path_of(self.benchmark.suites_root)

    @property
    def results_dir(self) -> Path:
        return self.path_of(self.output.results_dir)

    @property
    def reports_dir(self) -> Path:
        return self.path_of(self.output.dir)

    @property
    def snapshot_dir(self) -> Path:
        return self.path_of(self.evaluation.snapshot_dir)

    @property
    def workspace_root(self) -> Path:
        return self.path_of(self.runtime.workspace_root)

    def agent_spec_path(self, name: str) -> Path:
        return self.path_of(self.agents_dir) / f"{name}.yaml"

    # -- 校验 --------------------------------------------------------------
    def validate(self, *, allow_same_model: bool | None = None) -> None:
        """配置级校验（方案 B-04）。

        规则：
        1. 7 维权重求和必须 == 1.0（容差 1e-6），键必须都在 7 维枚举内。
        2. ``grader_model.name == model.name`` 且未开 ``allow_same_model`` → ``ConfigError``。
        3. ``grader_type`` 必须在枚举内；``pass_threshold`` 在 0~100。
        4. runs / concurrency / timeout 边界。
        """
        weights = self.evaluation.weights
        unknown = sorted(set(weights) - set(DIMENSIONS))
        if unknown:
            raise ConfigError(
                f"evaluation.weights 含未知维度：{', '.join(unknown)}；"
                f"合法维度为 {', '.join(DIMENSIONS)}"
            )
        total = sum(weights.values())
        if abs(total - 1.0) > _WEIGHT_TOL:
            raise ConfigError(
                f"evaluation.weights 求和必须为 1.0（容差 1e-6），当前为 {total:.6f}"
            )
        negative = sorted(k for k, v in weights.items() if v < 0)
        if negative:
            raise ConfigError(f"evaluation.weights 不允许负权重：{', '.join(negative)}")

        if self.evaluation.grader_type not in GRADER_TYPES:
            raise ConfigError(
                f"evaluation.grader_type 非法：{self.evaluation.grader_type}；"
                f"合法值：{', '.join(GRADER_TYPES)}"
            )
        if not 0.0 <= self.evaluation.pass_threshold <= 100.0:
            raise ConfigError(
                f"evaluation.pass_threshold 必须在 0~100，当前为 {self.evaluation.pass_threshold}"
            )
        if self.evaluation.runs < 1:
            raise ConfigError(f"evaluation.runs 必须 >= 1，当前为 {self.evaluation.runs}")
        if not 1 <= self.runtime.concurrency <= 4:
            raise ConfigError(
                f"runtime.concurrency 必须在 1~4（方案 1.4 上限 4），当前为 {self.runtime.concurrency}"
            )
        if self.runtime.timeout <= 0:
            raise ConfigError(f"runtime.timeout 必须为正整数，当前为 {self.runtime.timeout}")
        if self.runtime.timeout_grace_sec < 0:
            raise ConfigError("runtime.timeout_grace_sec 不能为负数")

        allow = self.allow_same_model if allow_same_model is None else allow_same_model
        self._check_model_separation(allow)

    def _check_model_separation(self, allow: bool) -> None:
        """评测模型与被测模型必须分离（方案 5.11 / R2）。"""
        mine = (self.model.name or "").strip().lower()
        judge = (self.grader_model.name or "").strip().lower()
        if not mine or not judge:
            return
        same_name = mine == judge
        same_base = (
            bool(self.model.base_url)
            and bool(self.grader_model.base_url)
            and self.model.base_url.strip().lower() == self.grader_model.base_url.strip().lower()
            and same_name
        )
        if (same_name or same_base) and not allow:
            raise ConfigError(
                "评测模型（grader_model.name）与被测模型（model.name）不得相同："
                f"两者均为 {self.grader_model.name!r}。"
                "请更换评测模型，或仅在 mock/echo 自测时设置 grader_model.allow_same_model: true"
            )

    def to_dict(self) -> dict[str, Any]:
        """**不含任何密钥**；只输出结构化字段，便于 ``--json`` 打印。"""
        return {
            "root": str(self.root),
            "benchmark": {
                "name": self.benchmark.name,
                "version": self.benchmark.version,
                "tasks_root": self.benchmark.tasks_root,
                "suites_root": self.benchmark.suites_root,
                "default_suite": self.benchmark.default_suite,
            },
            "agent": dict(self.agent),
            "model": {
                "provider": self.model.provider,
                "name": self.model.name,
                "base_url": self.model.base_url,
                "api_key_env": self.model.api_key_env,
                "temperature": self.model.temperature,
            },
            "grader_model": {
                "provider": self.grader_model.provider,
                "name": self.grader_model.name,
                "api_key_env": self.grader_model.api_key_env,
                "allow_same_model": self.allow_same_model,
            },
            "runtime": {
                "timeout": self.runtime.timeout,
                "timeout_grace_sec": self.runtime.timeout_grace_sec,
                "max_tool_calls": self.runtime.max_tool_calls,
                "concurrency": self.runtime.concurrency,
                "workspace_root": self.runtime.workspace_root,
                "keep_workspace": self.runtime.keep_workspace,
                "env_passthrough": list(self.runtime.env_passthrough),
                "fail_fast": self.runtime.fail_fast,
                "docker": copy.deepcopy(self.runtime.docker),
            },
            "evaluation": {
                "runs": self.evaluation.runs,
                "seed": self.evaluation.seed,
                "offline_replay": self.evaluation.offline_replay,
                "grader_type": self.evaluation.grader_type,
                "pass_threshold": self.evaluation.pass_threshold,
                "weights": dict(self.evaluation.weights),
            },
            "gates": copy.deepcopy(self.gates),
            "output": {
                "dir": self.output.dir,
                "format": list(self.output.format),
                "results_dir": self.output.results_dir,
            },
        }


def load_config(
    path: str | Path | None = None,
    *,
    root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
    validate: bool = True,
) -> Config:
    """加载配置。

    :param path: 配置文件路径；``None`` → ``<root>/config/default.yaml``。
    :param root: 项目根；``None`` → 自动向上查找 ``pyproject.toml``。
    :param env: 环境变量映射（便于单测注入，不污染进程环境）。
    :param overrides: 点号路径覆盖，如 ``{"evaluation.runs": 3}``。
    :param validate: 是否执行 :meth:`Config.validate`。
    """
    if root is None:
        root_path = find_project_root(Path(path).parent if path else None)
    else:
        root_path = Path(root).resolve()
    cfg_path = Path(path) if path else (root_path / "config" / "default.yaml")

    raw: dict[str, Any] = {}
    if cfg_path.is_file():
        raw = read_yaml_file(cfg_path)
    elif path is not None:
        raise ConfigError(f"配置文件不存在：{cfg_path}")

    raw = interpolate(raw, env)
    if overrides:
        raw = apply_overrides(raw, overrides)

    cfg = Config(
        root=root_path,
        path=cfg_path if cfg_path.is_file() else None,
        benchmark=BenchmarkConfig.from_dict(raw.get("benchmark")),
        model=ModelConfig.from_dict(raw.get("model")),
        grader_model=ModelConfig.from_dict(raw.get("grader_model")),
        runtime=RuntimeConfig.from_dict(raw.get("runtime")),
        evaluation=EvaluationConfig.from_dict(raw.get("evaluation")),
        gates=dict(raw.get("gates") or {}),
        output=OutputConfig.from_dict(raw.get("output")),
        agent=dict(raw.get("agent") or {}),
        raw=raw,
    )
    gm = raw.get("grader_model") or {}
    cfg.allow_same_model = bool(gm.get("allow_same_model", False))
    cfg.agents_dir = str(raw.get("agents_dir", "config/agents") or "config/agents")
    cfg.source_levels_path = str(
        raw.get("source_levels_path", "config/graders/source_levels.yaml")
        or "config/graders/source_levels.yaml"
    )
    cfg.semantic_path = str(
        raw.get("semantic_path", "config/graders/semantic_v1.yaml")
        or "config/graders/semantic_v1.yaml"
    )
    if validate:
        cfg.validate()
    return cfg


def apply_overrides(data: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """应用点号路径覆盖，返回新字典（不修改入参）。"""
    out = copy.deepcopy(dict(data))
    for dotted, value in overrides.items():
        parts = str(dotted).split(".")
        node: dict[str, Any] = out
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value
    return out


# --------------------------------------------------------------------------- #
# Agent / Grader 子配置
# --------------------------------------------------------------------------- #
def load_agent_spec(
    name_or_path: str | Path,
    *,
    config: Config | None = None,
    root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> AgentSpec:
    """加载 Agent 配置卡片。

    接受：Agent 名（``mock``）、文件路径（``config/agents/mock.yaml``）。
    """
    candidates: list[Path] = []
    given = Path(str(name_or_path))
    if given.suffix in (".yaml", ".yml"):
        if given.is_absolute():
            candidates.append(given)
        else:
            if root is not None:
                candidates.append(Path(root) / given)
            if config is not None:
                candidates.append(config.root / given)
            candidates.append(Path.cwd() / given)
    else:
        if config is not None:
            candidates.append(config.agent_spec_path(str(name_or_path)))
        search_roots = []
        if root is not None:
            search_roots.append(Path(root))
        search_roots.append(find_project_root())
        for base in search_roots:
            candidates.append(base / "config" / "agents" / f"{name_or_path}.yaml")

    for candidate in candidates:
        if candidate.is_file():
            raw = interpolate(read_yaml_file(candidate), env)
            spec = AgentSpec.from_dict(raw, source_path=str(candidate))
            if not spec.name:
                spec.name = candidate.stem
            return spec

    tried = ", ".join(str(c) for c in candidates[:3])
    raise ConfigError(
        f"找不到 Agent 配置 '{name_or_path}'（已尝试：{tried}）。"
        "内置类型可直接用 mock / echo；自定义 Agent 需在 config/agents/ 下新增 <name>.yaml"
    )


def list_agent_names(config: Config) -> list[str]:
    """列出 ``config/agents/*.yaml`` 的名字（按名称升序，保证确定性）。"""
    folder = config.path_of(config.agents_dir)
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.yaml"))


def load_source_levels(config: Config | None = None, path: str | Path | None = None) -> dict[str, Any]:
    """加载证据等级规则表（``config/graders/source_levels.yaml``）。"""
    target = Path(path) if path else (config.path_of(config.source_levels_path) if config else None)
    if target is None or not target.is_file():
        return {}
    return read_yaml_file(target)


def load_semantic_config(config: Config | None = None, path: str | Path | None = None) -> dict[str, Any]:
    """加载语义评测提示词配置（``config/graders/semantic_v1.yaml``）。"""
    target = Path(path) if path else (config.path_of(config.semantic_path) if config else None)
    if target is None or not target.is_file():
        return {}
    return read_yaml_file(target)


def load_suite_manifest(path: str | Path) -> Suite:
    """加载 suite manifest（5.1 格式）到 :class:`Suite`。"""
    p = Path(path)
    raw = read_yaml_file(p)
    suite_id = str(raw.get("id", "") or p.stem)
    spec = Suite(
        id=suite_id,
        split=str(raw.get("split", "public") or "public"),
        version=str(raw.get("version", "1.0") or "1.0"),
        description=str(raw.get("description", "") or ""),
        task_ids=raw.get("task_ids", "all") if raw.get("task_ids") is not None else "all",
        filters=dict(raw.get("filters") or {}),
        defaults=dict(raw.get("defaults") or {}),
        path=str(p),
    )
    if isinstance(spec.task_ids, str) and spec.task_ids != "all":
        # 允许 "A-001,B-002" 这种紧凑写法
        spec.task_ids = [t.strip() for t in spec.task_ids.split(",") if t.strip()]
    return spec


def check_weights(weights: Mapping[str, float], *, where: str = "weights") -> None:
    """校验权重字典（供 task 级权重覆盖复用，方案 5.1）。"""
    unknown = sorted(set(weights) - set(DIMENSIONS))
    if unknown:
        raise ConfigError(f"{where} 含未知维度：{', '.join(unknown)}")
    total = sum(float(v) for v in weights.values())
    if abs(total - 1.0) > _WEIGHT_TOL:
        raise ConfigError(f"{where} 求和必须为 1.0（容差 1e-6），当前为 {total:.6f}")


def iter_env_passthrough(names: Iterable[str], environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """按白名单从环境取变量；未设置的**不补空值**（缺失即不注入）。"""
    env = os.environ if environ is None else environ
    out: dict[str, str] = {}
    for name in names:
        value = env.get(name)
        if value is not None and value != "":
            out[name] = value
    return out
