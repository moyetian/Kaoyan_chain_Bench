"""C-03：受限 YAML 解析器（含真实配置文件回归）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from kaoyanbench.core.errors import ConfigError
from kaoyanbench.utils import mini_yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_scalars() -> None:
    data = mini_yaml.loads(
        "a: 1\nb: 1.5\nc: true\nd: false\ne: null\nf: hello\n"
    )
    assert data["a"] == 1
    assert data["b"] == 1.5
    assert data["c"] is True
    assert data["d"] is False
    assert data["e"] is None
    assert data["f"] == "hello"


def test_quoted_strings() -> None:
    data = mini_yaml.loads('a: "1.0"\nb: \'x: y\'\nc: "含 # 井号"\n')
    assert data["a"] == "1.0"
    assert data["b"] == "x: y"
    assert data["c"] == "含 # 井号"


def test_comment_stripped() -> None:
    data = mini_yaml.loads("a: 1  # 注释\n# 整行注释\nb: 2\n")
    assert data == {"a": 1, "b": 2}


def test_hash_in_unquoted_value_not_comment() -> None:
    # 值内的 # 只有在前面有空白时才当注释
    data = mini_yaml.loads("a: abc#def\n")
    assert data["a"] == "abc#def"


def test_nested_map() -> None:
    data = mini_yaml.loads("a:\n  b:\n    c: 1\n")
    assert data == {"a": {"b": {"c": 1}}}


def test_block_sequence() -> None:
    data = mini_yaml.loads("items:\n  - a\n  - b\n  - c\n")
    assert data["items"] == ["a", "b", "c"]


def test_sequence_of_maps() -> None:
    data = mini_yaml.loads("rules:\n  - pattern: x\n    level: E5\n  - pattern: y\n    level: E2\n")
    assert data["rules"] == [{"pattern": "x", "level": "E5"}, {"pattern": "y", "level": "E2"}]


def test_flow_map() -> None:
    data = mini_yaml.loads("g: { max_delta_pp: -5.0, level: fail }\n")
    assert data["g"] == {"max_delta_pp": -5.0, "level": "fail"}


def test_flow_map_no_space_after_colon() -> None:
    data = mini_yaml.loads("g:{ max_delta_pp: 3.0, level: fail }\n")
    assert data["g"] == {"max_delta_pp": 3.0, "level": "fail"}


def test_flow_seq() -> None:
    data = mini_yaml.loads("fmt: [json, markdown, html]\n")
    assert data["fmt"] == ["json", "markdown", "html"]


def test_flow_seq_no_space() -> None:
    data = mini_yaml.loads("fmt:[json,html]\n")
    assert data["fmt"] == ["json", "html"]


def test_empty_flow_seq() -> None:
    data = mini_yaml.loads("x: []\n")
    assert data["x"] == []


def test_url_value_kept() -> None:
    data = mini_yaml.loads("url: http://example.com/a:b\n")
    assert data["url"] == "http://example.com/a:b"


def test_unsupported_anchor_raises() -> None:
    with pytest.raises(ConfigError):
        mini_yaml.loads("a: &anchor 1\n")


def test_unsupported_block_scalar_raises() -> None:
    with pytest.raises(ConfigError):
        mini_yaml.loads("a: |\n  line1\n")


def test_error_has_line_number() -> None:
    with pytest.raises(mini_yaml.MiniYamlError) as exc:
        mini_yaml.loads("a: 1\n\tbad: 2\n")
    assert "行" in str(exc.value)


def test_tab_indent_rejected() -> None:
    with pytest.raises(ConfigError):
        mini_yaml.loads("a:\n\tb: 1\n")


def test_multi_document_rejected() -> None:
    with pytest.raises(ConfigError):
        mini_yaml.loads("a: 1\n---\nb: 2\n")


# --------------------------------------------------------------------------- #
# 真实配置文件回归（C-03 验收：不装 pyyaml 也能解析）
# --------------------------------------------------------------------------- #
def test_parse_real_default_yaml() -> None:
    data = mini_yaml.load(REPO_ROOT / "config" / "default.yaml")
    assert data["benchmark"]["name"] == "KaoyanBench"
    assert data["benchmark"]["version"] == "1.0"
    assert abs(sum(data["evaluation"]["weights"].values()) - 1.0) < 1e-9
    assert data["gates"]["task_success_rate"]["max_delta_pp"] == -5.0
    assert data["output"]["format"] == ["json", "markdown", "html"]


@pytest.mark.parametrize(
    "name",
    ["mock", "echo", "kaoyan_chain", "claude_code", "codex", "opencode", "workbuddy"],
)
def test_parse_real_agent_yaml(name: str) -> None:
    data = mini_yaml.load(REPO_ROOT / "config" / "agents" / f"{name}.yaml")
    assert data["name"] == name
    assert "type" in data


def test_parse_real_source_levels() -> None:
    data = mini_yaml.load(REPO_ROOT / "config" / "graders" / "source_levels.yaml")
    assert "default_level" in data
    assert isinstance(data["path_rules"], list)


def test_parse_real_semantic() -> None:
    data = mini_yaml.load(REPO_ROOT / "config" / "graders" / "semantic_v1.yaml")
    assert data["prompt_version"] == "v1"
    assert len(data["criteria"]) >= 1
