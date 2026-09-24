"""B-02：11 类错误枚举 + 规则化分类器。"""

from __future__ import annotations

import pytest

from kaoyanbench.core import errors as E


ALL_CODES = [
    "TIMEOUT",
    "HALLUCINATION",
    "SEARCH_FAILURE",
    "SOURCE_SELECTION_FAILURE",
    "PARSING_FAILURE",
    "TOOL_FAILURE",
    "PLANNING_FAILURE",
    "CONTEXT_FAILURE",
    "CITATION_FAILURE",
    "CALCULATION_FAILURE",
    "UNKNOWN",
]


def test_error_code_enum_complete() -> None:
    values = {c.value for c in E.ErrorCode}
    for code in ALL_CODES:
        assert code in values, f"缺少错误码 {code}"
    assert len(values) == 11


def test_labels_defined_for_all() -> None:
    for code in E.ERROR_CODES:
        assert code in E.ERROR_LABELS
        assert E.ERROR_LABELS[code]


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"timed_out": True}, "TIMEOUT"),
        ({"hallucination_violations": 2}, "HALLUCINATION"),
        ({"network": "online", "has_sources": False}, "SEARCH_FAILURE"),
        (
            {"network": "online", "has_sources": True, "required_sources": 2, "required_sources_satisfied": 0},
            "SEARCH_FAILURE",
        ),
        ({"parse_failed": True}, "PARSING_FAILURE"),
        ({"category": "planning"}, "PLANNING_FAILURE"),
        ({"canary_missed": True}, "CONTEXT_FAILURE"),
        ({"numeric_check_failed": True}, "CALCULATION_FAILURE"),
        ({"citation_accuracy": 0.3, "citation_count": 3}, "CITATION_FAILURE"),
        ({"tool_calls": 5, "tool_success_rate": 0.4}, "TOOL_FAILURE"),
        ({"has_sources": True, "source_precision": 0.2}, "SOURCE_SELECTION_FAILURE"),
    ],
)
def test_classify_each_kind(kwargs: dict, expected: str) -> None:
    finding = E.ClassificationInput(**kwargs)
    out = [f.code for f in E.classify(finding)]
    assert expected in out, f"{kwargs} → {out}"


def test_ground_truth_conflict_is_hallucination() -> None:
    finding = E.ClassificationInput(ground_truth_conflict=True)
    out = [f.code for f in E.classify(finding)]
    assert "HALLUCINATION" in out


def test_classify_multiple_hits_priority() -> None:
    finding = E.ClassificationInput(timed_out=True, parse_failed=True, numeric_check_failed=True)
    out = [f.code for f in E.classify(finding)]
    assert "TIMEOUT" in out and "PARSING_FAILURE" in out and "CALCULATION_FAILURE" in out
    # TIMEOUT 在 RULES 中排最前，优先级最高
    assert out.index("TIMEOUT") == 0


def test_classify_no_hits_returns_empty() -> None:
    assert E.classify(E.ClassificationInput()) == []


def test_classify_or_unknown_no_hits_no_signs() -> None:
    # 正常运行（无异常迹象）不应产出 UNKNOWN
    assert E.classify_or_unknown(E.ClassificationInput()) == []


def test_classify_or_unknown_with_anomaly_signs() -> None:
    # 有工具调用但采不到成功率 → 异常迹象且无规则命中 → UNKNOWN
    out = [f.code for f in E.classify_or_unknown(E.ClassificationInput(tool_calls=3))]
    assert out == ["UNKNOWN"]


def test_finding_to_error_item() -> None:
    finding = E.RawFinding("TIMEOUT", "硬超时")
    item = E.finding_to_error_item(
        finding, task_id="T-1", run_id="r1", at="2026-01-01T00:00:00Z"
    )
    assert item.code == "TIMEOUT"
    assert item.task_id == "T-1"
    assert item.run_id == "r1"


def test_error_counts_all_codes_present() -> None:
    counts = E.error_counts([])
    for code in E.ERROR_CODES:
        assert code in counts
        assert counts[code] == 0


def test_error_counts_ranked_desc() -> None:
    f1 = E.finding_to_error_item(E.RawFinding("TIMEOUT", "t"), task_id="T", run_id="r", at="x")
    f2 = E.finding_to_error_item(E.RawFinding("SEARCH_FAILURE", "s"), task_id="T", run_id="r", at="x")
    f3 = E.finding_to_error_item(E.RawFinding("SEARCH_FAILURE", "s"), task_id="T", run_id="r", at="x")
    ranked = E.error_counts_ranked([f1, f2, f3])
    assert ranked[0]["code"] == "SEARCH_FAILURE"
    assert ranked[0]["count"] == 2


def test_unknown_code_enum() -> None:
    assert E.ErrorCode.UNKNOWN.value == "UNKNOWN"


def test_reason_labels_present() -> None:
    for key in ("hard_timeout", "no_sources", "calculation_mismatch"):
        assert key in E.REASON_LABELS
