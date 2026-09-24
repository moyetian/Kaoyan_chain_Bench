"""B-11：16 种声明式 check（每种通过 / 不通过各一）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaoyanbench.core.checks import run_check
from kaoyanbench.core.models import (
    AgentOutput,
    AnswerFormat,
    Check,
    Citation,
    Environment,
    Expected,
    GraderSpec,
    Point,
    Source,
    Task,
)


def make_output(**kwargs) -> AgentOutput:
    base = dict(
        run_id="r1",
        agent="mock",
        model="m",
        task_id="T-1",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
        duration_ms=1000,
        final_answer="",
        environment=Environment(),
    )
    base.update(kwargs)
    return AgentOutput(**base)


def make_task(**kwargs) -> Task:
    base = dict(
        task_id="T-1",
        category="search",
        difficulty="easy",
        instruction="x",
        expected=Expected(),
        grader=GraderSpec(type="deterministic"),
    )
    base.update(kwargs)
    return Task(**base)


class Ctx:
    """最小 GradeContext 替身。"""

    def __init__(self, workspace: Path | None = None, **kw):
        self.workspace_dir = Path(workspace) if workspace else Path("/tmp/nonexistent")
        self.offline_replay = kw.get("offline_replay", False)
        self.snapshot_dir = kw.get("snapshot_dir")
        self.source_levels = kw.get("source_levels", {})
        self.now = kw.get("now", "2026-01-01T00:00:00Z")


# --------------------------------------------------------------------------- #
def test_point_hit_pass_and_fail() -> None:
    task = make_task(expected=Expected(must_find=[Point(id="p1", any_of=["招生"], all_of=[])]))
    check = Check(id="c1", type="point_hit", dimension="factuality", params={"point_id": "p1"})
    ok = run_check(check, task, make_output(final_answer="今年招生信息如下"), Ctx())
    assert ok.passed is True
    bad = run_check(check, task, make_output(final_answer="无关内容"), Ctx())
    assert bad.passed is False


def test_numeric_pass_and_fail() -> None:
    check = Check(
        id="c1", type="numeric", dimension="factuality",
        params={"path": "$.final_answer", "value": 42, "op": "eq"},
    )
    assert run_check(check, make_task(), make_output(final_answer="42"), Ctx()).passed is True
    assert run_check(check, make_task(), make_output(final_answer="41"), Ctx()).passed is False


def test_string_eq_pass_and_fail() -> None:
    check = Check(
        id="c1", type="string_eq", dimension="factuality",
        params={"path": "$.final_answer", "value": "yes"},
    )
    assert run_check(check, make_task(), make_output(final_answer="yes"), Ctx()).passed is True
    assert run_check(check, make_task(), make_output(final_answer="no"), Ctx()).passed is False


def test_string_contains_pass_and_fail() -> None:
    check = Check(id="c1", type="string_contains", dimension="factuality", params={"value": "复试"})
    assert run_check(check, make_task(), make_output(final_answer="关于复试线"), Ctx()).passed is True
    assert run_check(check, make_task(), make_output(final_answer="无关"), Ctx()).passed is False


def test_regex_pass_and_fail() -> None:
    check = Check(id="c1", type="regex", dimension="factuality", params={"pattern": r"20\d{2}"})
    assert run_check(check, make_task(), make_output(final_answer="2025年"), Ctx()).passed is True
    assert run_check(check, make_task(), make_output(final_answer="无年份"), Ctx()).passed is False


def test_set_includes_pass_and_fail() -> None:
    check = Check(
        id="c1", type="set_includes", dimension="completeness",
        params={"values": ["a", "b"], "min_hits": 2},
    )
    assert run_check(check, make_task(), make_output(final_answer="a 和 b 都有"), Ctx()).passed is True
    assert run_check(check, make_task(), make_output(final_answer="只有 a"), Ctx()).passed is False


def test_json_schema_pass_and_fail() -> None:
    check = Check(
        id="c1", type="json_schema", dimension="completeness",
        params={"schema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}},
    )
    ok = run_check(check, make_task(), make_output(final_answer='{"name": "x"}'), Ctx())
    assert ok.passed is True
    bad = run_check(check, make_task(), make_output(final_answer='{"age": 1}'), Ctx())
    assert bad.passed is False


def test_file_exists_pass_and_fail(tmp_path: Path) -> None:
    (tmp_path / "out.txt").write_text("hi", encoding="utf-8")
    check = Check(id="c1", type="file_exists", dimension="completeness", params={"filename": "out.txt"})
    assert run_check(check, make_task(), make_output(), Ctx(tmp_path)).passed is True
    check2 = Check(id="c2", type="file_exists", dimension="completeness", params={"filename": "missing.txt"})
    assert run_check(check2, make_task(), make_output(), Ctx(tmp_path)).passed is False


def test_file_json_match_pass_and_fail(tmp_path: Path) -> None:
    (tmp_path / "o.json").write_text(json.dumps({"n": 5}), encoding="utf-8")
    check = Check(
        id="c1", type="file_json_match", dimension="completeness",
        params={"filename": "o.json", "path": "$.n", "value": 5, "op": "eq"},
    )
    assert run_check(check, make_task(), make_output(), Ctx(tmp_path)).passed is True
    bad = Check(
        id="c2", type="file_json_match", dimension="completeness",
        params={"filename": "o.json", "path": "$.n", "value": 6, "op": "eq"},
    )
    assert run_check(bad, make_task(), make_output(), Ctx(tmp_path)).passed is False


def test_year_tag_pass_and_fail() -> None:
    check = Check(id="c1", type="year_tag", dimension="factuality", params={"year": 2025})
    assert run_check(check, make_task(), make_output(final_answer="2025 年"), Ctx()).passed is True
    assert run_check(check, make_task(), make_output(final_answer="2020 年"), Ctx()).passed is False


def test_source_domain_pass_and_fail() -> None:
    check = Check(
        id="c1", type="source_domain", dimension="source_quality",
        params={"domain_suffix": [".edu.cn"], "min_count": 1},
    )
    src = [Source(url="https://a.example.edu.cn/x", domain="a.example.edu.cn", title="t")]
    assert run_check(check, make_task(), make_output(sources=src), Ctx()).passed is True
    src2 = [Source(url="https://weibo.com/x", domain="weibo.com", title="t")]
    assert run_check(check, make_task(), make_output(sources=src2), Ctx()).passed is False


def test_source_level_pass_and_fail() -> None:
    check = Check(id="c1", type="source_level", dimension="source_quality", params={"level_min": "E4", "min_count": 1})
    hi = [Source(url="u", domain="a.edu.cn", title="t", evidence_level="E5")]
    lo = [Source(url="u", domain="x.com", title="t", evidence_level="E2")]
    assert run_check(check, make_task(), make_output(sources=hi), Ctx()).passed is True
    assert run_check(check, make_task(), make_output(sources=lo), Ctx()).passed is False


def test_citation_coverage_undetermined_when_unjudged() -> None:
    check = Check(
        id="c1", type="citation_coverage", dimension="citation",
        params={"min_citations": 1, "min_supported_ratio": 0.5},
    )
    cites = [Citation(citation_id="c1", claim="x", source_ref="s1")]  # supported=None
    result = run_check(check, make_task(), make_output(citations=cites), Ctx())
    assert result.passed is None  # 未判定 → None，不计入均值

    judged_ok = [Citation(citation_id="c1", claim="x", source_ref="s1", supported=True)]
    assert run_check(check, make_task(), make_output(citations=judged_ok), Ctx()).passed is True
    judged_bad = [Citation(citation_id="c1", claim="x", source_ref="s1", supported=False)]
    assert run_check(check, make_task(), make_output(citations=judged_bad), Ctx()).passed is False


def test_must_not_claim_forces_critical() -> None:
    task = make_task(expected=Expected(must_not_claim=["保证录取"]))
    check = Check(id="c1", type="must_not_claim", dimension="factuality", params={})
    hit = run_check(check, task, make_output(final_answer="我们保证录取"), Ctx())
    assert hit.passed is False
    assert hit.critical is True
    clean = run_check(check, task, make_output(final_answer="仅供参考"), Ctx())
    assert clean.passed is True


def test_constraint_pass_and_fail() -> None:
    schedule = {
        "schedule": [
            {"day": 1, "slots": [{"subject": "math", "hours": 3}]},
            {"day": 2, "slots": [{"subject": "english", "hours": 2}]},
        ]
    }
    check = Check(
        id="c1", type="constraint", dimension="completeness",
        params={"answer_path": "$", "rules": [{"kind": "total_days", "op": "eq", "value": 2}]},
    )
    assert run_check(check, make_task(), make_output(final_answer=json.dumps(schedule)), Ctx()).passed is True
    bad = Check(
        id="c2", type="constraint", dimension="completeness",
        params={"answer_path": "$", "rules": [{"kind": "total_days", "op": "eq", "value": 9}]},
    )
    assert run_check(bad, make_task(), make_output(final_answer=json.dumps(schedule)), Ctx()).passed is False


def test_evidence_level_pass_and_fail() -> None:
    check = Check(
        id="c1", type="evidence_level", dimension="source_quality",
        params={"path": "$.sources", "expected_min": "E4"},
    )
    hi = [Source(url="u", domain="a.edu.cn", title="t", evidence_level="E5")]
    lo = [Source(url="u", domain="x.com", title="t", evidence_level="E2")]
    assert run_check(check, make_task(), make_output(sources=hi), Ctx()).passed is True
    assert run_check(check, make_task(), make_output(sources=lo), Ctx()).passed is False


def test_unknown_check_type_returns_none() -> None:
    check = Check(id="c1", type="nope", dimension="factuality", params={})
    assert run_check(check, make_task(), make_output(), Ctx()).passed is None


def test_check_exception_isolated() -> None:
    # json_schema 收到非法 schema 不应抛异常
    check = Check(id="c1", type="json_schema", dimension="completeness", params={"schema": 5})
    result = run_check(check, make_task(), make_output(final_answer="{}"), Ctx())
    assert result.passed in (None, True, False)  # 不崩即可


# --------------------------------------------------------------------------- #
# BUG-3 回归：json_schema 的 minLength/maxLength 对 array 按元素个数判定
# --------------------------------------------------------------------------- #
def _json_schema_check(schema, path=None) -> Check:
    params = {"schema": schema}
    if path:
        params["path"] = path
    return Check(id="c1", type="json_schema", dimension="completeness", params=params)


def test_json_schema_array_minLength_enforced() -> None:
    from kaoyanbench.core.checks import validate_json_schema

    schema = {"type": "array", "minLength": 3, "items": {"type": "object", "required": ["year"]}}
    # 长度不足 → fail
    problems = validate_json_schema([{"year": 2024}, {"year": 2025}], schema)
    assert problems, "array 长度 2 < minLength 3 应判 fail"
    # 长度足够 → pass
    assert validate_json_schema([{"year": 1}, {"year": 2}, {"year": 3}], schema) == []


def test_json_schema_array_maxLength_enforced() -> None:
    from kaoyanbench.core.checks import validate_json_schema

    schema = {"type": "array", "maxLength": 2}
    assert validate_json_schema([1, 2, 3], schema), "array 长度 3 > maxLength 2 应判 fail"
    assert validate_json_schema([1, 2], schema) == []


def test_json_schema_array_minItems_maxItems_supported() -> None:
    from kaoyanbench.core.checks import validate_json_schema

    assert validate_json_schema([1], {"type": "array", "minItems": 2})
    assert validate_json_schema([1, 2, 3], {"type": "array", "maxItems": 2})


def test_json_schema_str_length_semantics_unchanged() -> None:
    from kaoyanbench.core.checks import validate_json_schema

    # str 仍按字符数：边界不变
    assert validate_json_schema("ab", {"type": "string", "minLength": 3})
    assert validate_json_schema("abc", {"type": "string", "minLength": 3}) == []
    assert validate_json_schema("abcd", {"type": "string", "maxLength": 3})
    assert validate_json_schema("abc", {"type": "string", "maxLength": 3}) == []


def test_json_schema_array_end_to_end_via_run_check() -> None:
    """端到端：answer 是 JSON 数组，经 run_check 判定。"""
    check = _json_schema_check(
        {"type": "array", "minLength": 2, "items": {"type": "object", "required": ["year", "total"]}},
        path="$.rows",
    )
    bad = run_check(check, make_task(), make_output(final_answer='{"rows": [{"year": 2024, "total": 10}]}'), Ctx())
    assert bad.passed is False
    good = run_check(
        check, make_task(),
        make_output(final_answer='{"rows": [{"year": 2024, "total": 10}, {"year": 2025, "total": 20}]}'),
        Ctx(),
    )
    assert good.passed is True


# --------------------------------------------------------------------------- #
# P3 评测发现：模型常给 JSON 加围栏 / 前后缀解释，解析器应宽容提取（对所有被测公平）
def test_parse_json_text_tolerates_fences_and_affixes() -> None:
    from kaoyanbench.core.checks import _parse_json_text, answer_payload

    assert _parse_json_text('{"count": 10}') == {"count": 10}
    assert _parse_json_text('```json\n{"count": 10}\n```') == {"count": 10}
    assert _parse_json_text('```\n{"count": 10}\n```') == {"count": 10}
    assert _parse_json_text('结果如下：{"count": 10}，请查收') == {"count": 10}
    assert _parse_json_text('纯文本答案') is None
    out = make_output(final_answer='```json\n{"count": 10}\n```')
    assert answer_payload(out)["count"] == 10


def test_numeric_passes_on_fenced_json() -> None:
    check = Check(
        id="c1", type="numeric", dimension="factuality",
        params={"path": "$.count", "value": 10, "op": "eq", "tol": 0.0},
    )
    out = make_output(final_answer='```json\n{"count": 10, "subject": "x"}\n```')
    assert run_check(check, make_task(), out, Ctx()).passed is True
