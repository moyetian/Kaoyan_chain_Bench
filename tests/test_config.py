"""B-04：配置加载、环境变量插值、权重与模型分离校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from kaoyanbench.core.config import (
    Config,
    interpolate,
    list_agent_names,
    load_agent_spec,
    load_config,
    load_semantic_config,
    load_source_levels,
    load_suite_manifest,
)
from kaoyanbench.core.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_load_default_config() -> None:
    cfg = load_config(REPO_ROOT / "config" / "default.yaml", root=REPO_ROOT)
    assert cfg.benchmark.name == "KaoyanBench"
    assert cfg.benchmark.version == "1.0"
    assert abs(sum(cfg.evaluation.weights.values()) - 1.0) < 1e-9
    assert cfg.evaluation.pass_threshold == 60.0


def test_interpolate_default() -> None:
    out = interpolate("${MISSING_VAR:-fallback}", env={})
    assert out == "fallback"


def test_interpolate_env_value() -> None:
    out = interpolate("${MY_VAR}", env={"MY_VAR": "value"})
    assert out == "value"


def test_interpolate_in_nested_structure() -> None:
    raw = {"a": {"b": "${X:-d}"}, "c": ["${X:-d}", 1]}
    out = interpolate(raw, env={})
    assert out["a"]["b"] == "d"
    assert out["c"][0] == "d"


def test_env_interpolation_in_real_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com/v1")
    raw = (REPO_ROOT / "config" / "default.yaml").read_text(encoding="utf-8")
    # 通过配置加载器读取（read_yaml_file 内部会插值）
    cfg = load_config(REPO_ROOT / "config" / "default.yaml", root=REPO_ROOT)
    assert cfg.model.base_url == "https://api.example.com/v1"


def test_weights_not_sum_one_raises(tmp_path: Path) -> None:
    text = (
        "benchmark:\n  name: X\n  version: \"1.0\"\n"
        "model:\n  name: m\n"
        "grader_model:\n  name: g\n"
        "evaluation:\n  weights:\n"
        "    factuality: 0.5\n    source_quality: 0.2\n    citation: 0.1\n"
        "    completeness: 0.1\n    tool_execution: 0.1\n    task_completion: 0.1\n"
        "    efficiency: 0.1\n"
    )
    path = tmp_path / "bad.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, root=tmp_path)


def test_same_model_raises_when_not_allowed(tmp_path: Path) -> None:
    text = (
        "benchmark:\n  name: X\n  version: \"1.0\"\n"
        "model:\n  name: same-model\n"
        "grader_model:\n  name: same-model\n  allow_same_model: false\n"
        "evaluation:\n  weights:\n"
        "    factuality: 0.3\n    source_quality: 0.2\n    citation: 0.15\n"
        "    completeness: 0.15\n    tool_execution: 0.1\n    task_completion: 0.05\n"
        "    efficiency: 0.05\n"
    )
    path = tmp_path / "same.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, root=tmp_path)


def test_same_model_allowed(tmp_path: Path) -> None:
    text = (
        "benchmark:\n  name: X\n  version: \"1.0\"\n"
        "model:\n  name: same-model\n"
        "grader_model:\n  name: same-model\n  allow_same_model: true\n"
        "evaluation:\n  weights:\n"
        "    factuality: 0.3\n    source_quality: 0.2\n    citation: 0.15\n"
        "    completeness: 0.15\n    tool_execution: 0.1\n    task_completion: 0.05\n"
        "    efficiency: 0.05\n"
    )
    path = tmp_path / "same_ok.yaml"
    path.write_text(text, encoding="utf-8")
    cfg = load_config(path, root=tmp_path)
    assert cfg.grader_model.name == "same-model"


def test_list_agent_names() -> None:
    cfg = load_config(REPO_ROOT / "config" / "default.yaml", root=REPO_ROOT)
    names = list_agent_names(cfg)
    for expected in ("mock", "echo", "kaoyan_chain"):
        assert expected in names


def test_load_agent_spec_by_name() -> None:
    cfg = load_config(REPO_ROOT / "config" / "default.yaml", root=REPO_ROOT)
    spec = load_agent_spec("mock", config=cfg)
    assert spec.name == "mock"
    assert spec.type == "mock"


def test_load_agent_spec_missing_raises() -> None:
    cfg = load_config(REPO_ROOT / "config" / "default.yaml", root=REPO_ROOT)
    with pytest.raises(ConfigError):
        load_agent_spec("does_not_exist", config=cfg)


def test_load_source_levels_uses_config() -> None:
    cfg = load_config(REPO_ROOT / "config" / "default.yaml", root=REPO_ROOT)
    data = load_source_levels(cfg)
    assert data["default_level"] == "E2"


def test_load_semantic_config_uses_config() -> None:
    cfg = load_config(REPO_ROOT / "config" / "default.yaml", root=REPO_ROOT)
    data = load_semantic_config(cfg)
    assert data["prompt_version"] == "v1"


def test_load_suite_manifest() -> None:
    suite = load_suite_manifest(REPO_ROOT / "benchmark" / "suites" / "smoke.yaml")
    assert suite.id == "smoke"
    assert suite.split == "public"


def test_output_dirs_are_absolute() -> None:
    cfg = load_config(REPO_ROOT / "config" / "default.yaml", root=REPO_ROOT)
    assert cfg.tasks_root.is_absolute()
    assert cfg.results_dir.is_absolute()
    assert cfg.reports_dir.is_absolute()
