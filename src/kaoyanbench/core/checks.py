"""16 种声明式 check 的实现（方案 5.3 / B-11）。

设计要点：
- 每种 check 是**纯函数**：``(check, task, output, ctx) -> CheckResult``。
- ``passed`` 三态：``True`` / ``False`` / ``None``（``None`` = 无法判定，不计分子分母）。
- ``must_not_claim`` 命中即记幻觉违规（``critical`` 强制为 True）。
- 所有文件类 check 都读 **workspace 内的产出**，不读任务目录，避免越权读题目自带答案。
- 数值比较统一走 :func:`~kaoyanbench.utils.text.get_by_path` 的 JSONPath 子集。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..utils.text import (
    approx_equal,
    contains_all,
    contains_any,
    count_hits,
    get_by_path,
    normalize,
    safe_str,
)
from .errors import GraderError
from .evidence import count_at_least, domain_matches, level_rank
from .models import (
    AgentOutput,
    Check,
    CheckResult,
    Source,
)

__all__ = [
    "HANDLERS",
    "run_check",
    "check_point_hit",
    "check_numeric",
    "check_string_eq",
    "check_string_contains",
    "check_regex",
    "check_set_includes",
    "check_json_schema",
    "check_file_exists",
    "check_file_json_match",
    "check_year_tag",
    "check_source_domain",
    "check_source_level",
    "check_citation_coverage",
    "check_must_not_claim",
    "check_constraint",
    "check_evidence_level",
    "answer_payload",
    "validate_json_schema",
    "ConstraintEvaluator",
]

_TOL_DEFAULT = 1e-6


# --------------------------------------------------------------------------- #
# 上下文工具
# --------------------------------------------------------------------------- #
def _ctx() -> Any:
    """延迟导入 ``GradeContext``（避免 models ↔ checks 循环）。"""
    from .grader import GradeContext

    return GradeContext


def answer_payload(output: AgentOutput, check: Check | None = None) -> Any:
    """待检查的数据源：优先取 ``output.raw`` 里的解析结果，否则用 final_answer。

    ``path`` 为 ``$.answer`` 时，先看 ``raw['answer']``，再看 ``answer_files`` 里的 JSON，
    最后回退到把 final_answer 当 JSON 解析。
    """
    raw = output.raw or {}
    if isinstance(raw, Mapping):
        for key in ("parsed", "data", "answer_json"):
            candidate = raw.get(key)
            if isinstance(candidate, (dict, list)):
                return candidate
    # 约定的 answer.json
    for name, path in (output.answer_files or {}).items():
        if str(name).lower() in ("answer.json", "final_answer.json"):
            loaded = _load_json_rel(output, path)
            if loaded is not None:
                return loaded
    parsed = _parse_json_text(output.final_answer)
    if parsed is not None:
        return parsed
    return {"answer": output.final_answer, "final_answer": output.final_answer}


def _parse_json_text(text: str) -> Any:
    stripped = (text or "").strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _load_json_rel(output: AgentOutput, rel_path: str) -> Any:
    """按 ``answer_files`` 的相对路径读取 JSON（路径已由 Runner 限定在 workspace 内）。"""
    text = _read_text_rel(output, rel_path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _read_text_rel(output: AgentOutput, rel_path: str) -> str | None:
    """读取产出文件文本；拒绝路径穿越（``..``）。"""
    if not rel_path:
        return None
    pure = Path(str(rel_path))
    if pure.is_absolute() or ".." in pure.parts:
        return None
    base = getattr(output, "_workspace_dir", None)
    if base is None:
        return None
    target = Path(base) / pure
    if not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _workspace_dir(output: AgentOutput, ctx: Any | None) -> None:
    """把 workspace 目录挂到 output 上（仅供本次 grade 使用，不入库）。"""
    if ctx is not None and getattr(ctx, "workspace_dir", None):
        try:
            object.__setattr__(output, "_workspace_dir", Path(ctx.workspace_dir))
        except (AttributeError, TypeError):  # pragma: no cover - 非 dataclass
            pass


def _first_file(output: AgentOutput, patterns: Sequence[str]) -> tuple[str, str] | None:
    """在 answer_files 中按文件名模式找第一个匹配，返回 ``(文件名, 内容)``。"""
    names = sorted(output.answer_files or {})
    for pattern in patterns:
        compiled = re.compile(pattern, re.IGNORECASE)
        for name in names:
            if compiled.search(name):
                text = _read_text_rel(output, output.answer_files[name])
                if text is not None:
                    return name, text
    return None


def _replay_task_year(task: Any) -> int | None:
    """从任务的 required_sources 取年份要求（与 grader.attach_evidence_levels 同口径）。"""
    try:
        reqs = list(getattr(getattr(task, "expected", None), "required_sources", None) or [])
    except (AttributeError, TypeError):
        return None
    for req in reqs:
        year = getattr(req, "year", None)
        if year is None and isinstance(req, Mapping):
            year = req.get("year")
        if year is not None:
            try:
                return int(year)
            except (TypeError, ValueError):
                return None
    return None


def _annotate_replayed(sources: list[Source], task: Any, ctx: Any) -> list[Source]:
    """用证据规则重判定回放来源（P2 修复）。

    ``sources_from_snapshots`` 产出的 Source 证据等级恒为 E0（仅搬运索引，
    无规则上下文）；若不重判定，``--offline-replay`` 下所有 source_level
    检查恒失败，快照库形同虚设。这里沿用 ``ctx`` 的证据规则集
    （``evidence_rules`` 属性，不存在时回退 ``source_levels`` 字典）
    与任务年份做原地标注（E0 → E4/E5），与正常路径的
    ``attach_evidence_levels`` 等价。
    """
    from .evidence import judge_sources, load_rules

    rules = getattr(ctx, "evidence_rules", None)
    if rules is None:
        data = getattr(ctx, "source_levels", None)
        try:
            rules = load_rules(data if isinstance(data, Mapping) else None)
        except (AttributeError, TypeError, ValueError):  # pragma: no cover - 规则损坏时保守降级
            return sources
    try:
        return judge_sources(sources, rules, task_year=_replay_task_year(task), annotate=True)
    except (AttributeError, TypeError, ValueError):  # pragma: no cover - 判定异常不阻断评分
        return sources


def _sources_of(output: AgentOutput, ctx: Any, task: Any = None) -> list[Source]:
    """取来源列表（``offline_replay`` 时只用**本任务**快照内的来源）。

    必须限定 ``task_id``：否则会把快照目录下所有任务的来源都算进来，
    使 ``source_precision`` / ``source_level`` / ``citation_coverage`` 偏乐观。
    拿不到 ``task_id`` 时不回放（宁可用 Agent 自报来源，也不跨任务污染）。
    回放来源会经证据规则重判定（见 :func:`_annotate_replayed`），否则恒为 E0。
    """
    from .snapshot import sources_from_snapshots

    if getattr(ctx, "offline_replay", False) and getattr(ctx, "snapshot_dir", None):
        task_id = str(getattr(ctx, "task_id", "") or "").strip()
        if task_id:
            replayed = sources_from_snapshots(Path(ctx.snapshot_dir), task_id=task_id)
            if replayed:
                return _annotate_replayed(replayed, task, ctx)
    return list(output.sources or [])


def _result(
    check: Check,
    passed: bool | None,
    detail: str,
    *,
    judge: str = "deterministic",
) -> CheckResult:
    return CheckResult(
        id=check.id,
        type=check.type,
        dimension=check.dimension or "completeness",
        passed=passed,
        weight=float(check.weight),
        critical=bool(check.critical),
        detail=detail,
        judge=judge,
    )


# --------------------------------------------------------------------------- #
# 1. point_hit
# --------------------------------------------------------------------------- #
def check_point_hit(check: Check, task: TaskLike, output: AgentOutput, ctx: Any) -> CheckResult:
    """要点命中：``any_of`` 任一命中 **且** ``all_of`` 全部命中。

    支持 ``point_id`` 引用 ``task.expected.must_find``（优先）。
    """
    point = None
    point_id = check.get("point_id")
    if point_id:
        for candidate in task.expected.must_find:
            if candidate.id == point_id:
                point = candidate
                break
        if point is None:
            return _result(check, None, f"point_id 不存在：{point_id}")

    haystack = _point_haystack(output)
    any_of = list(check.get("any_of") or (point.any_of if point else []) or [])
    all_of = list(check.get("all_of") or (point.all_of if point else []) or [])
    if not any_of and not all_of:
        return _result(check, None, "既无 any_of 也无 all_of，无法判定")

    if all_of and not contains_all(haystack, all_of):
        missing = [x for x in all_of if not contains_all(haystack, [x])]
        return _result(check, False, f"all_of 未全部命中，缺失：{'、'.join(missing)}")
    if any_of and not contains_any(haystack, any_of):
        return _result(check, False, f"any_of 均未命中：{'、'.join(any_of)}")
    hit = next((x for x in any_of if contains_any(haystack, [x])), "")
    label = f"命中：{hit}" if hit else "命中：all_of 全部满足"
    if point:
        label = f"{label}（要点 {point.id}）"
    return _result(check, True, label)


def _point_haystack(output: AgentOutput) -> str:
    """要点匹配语料：final_answer + 全部产出文件（含 JSON 键值）。"""
    parts = [output.final_answer or ""]
    for name in sorted(output.answer_files or {}):
        text = _read_text_rel(output, output.answer_files[name])
        if text:
            parts.append(text)
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 2. numeric
# --------------------------------------------------------------------------- #
_OPS: dict[str, Callable[[float, float], bool]] = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
}


def check_numeric(check: Check, task: TaskLike, output: AgentOutput, ctx: Any) -> CheckResult:
    """数值比对：``op`` ∈ eq/ne/gt/ge/lt/le，``tol`` 为容差（仅 eq/ne 生效）。"""
    path = check.get("path")
    expected = check.get("value")
    op = str(check.get("op", "eq") or "eq").lower()
    tol = float(check.get("tol", _TOL_DEFAULT) or 0.0)
    if op not in _OPS:
        return _result(check, None, f"不支持的 op：{op}")
    data = _resolve(check, output, ctx)
    actual = get_by_path(data, path)
    if actual is None and path in ("answer", "$.answer", "final_answer", "$.final_answer"):
        actual = get_by_path(data, None)
    number = _to_number(actual)
    if number is None:
        return _result(check, False, f"路径 {path or '$'} 未取到数值（实际：{safe_str(actual)[:60]}）")
    target = _to_number(expected)
    if target is None:
        return _result(check, None, f"check.value 不是数字：{safe_str(expected)}")
    if op in ("eq", "ne"):
        passed = approx_equal(number, target, tol)
        if op == "ne":
            passed = not passed
    else:
        passed = _OPS[op](number, target)
    return _result(
        check,
        passed,
        f"{path or '$'} = {number:g}，期望 {op} {target:g}" + (f"（tol={tol:g}）" if op == "eq" else ""),
    )


def _to_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = safe_str(value).strip().replace(",", "").replace("%", "")
    try:
        return float(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 3/4/5. string_eq / string_contains / regex
# --------------------------------------------------------------------------- #
def check_string_eq(check: Check, task: TaskLike, output: AgentOutput, ctx: Any) -> CheckResult:
    data = _resolve(check, output, ctx)
    path = check.get("path")
    actual = get_by_path(data, path)
    expected = check.get("value")
    case_sensitive = bool(check.get("case_sensitive", False))
    if actual is None:
        return _result(check, False, f"路径 {path or '$'} 未取到值")
    a = safe_str(actual)
    b = safe_str(expected)
    if not case_sensitive:
        a_norm, b_norm = normalize(a), normalize(b)
    else:
        a_norm, b_norm = a, b
    return _result(check, a_norm == b_norm, f"{path or '$'} = {a[:60]!r}，期望 {b[:60]!r}")


def check_string_contains(check: Check, task: TaskLike, output: AgentOutput, ctx: Any) -> CheckResult:
    data = _resolve(check, output, ctx)
    path = check.get("path")
    actual = get_by_path(data, path) if path else data
    value = check.get("value")
    text = safe_str(actual)
    if not text:
        return _result(check, False, f"路径 {path or '$'} 为空，无法判断包含")
    hit = value in text if check.get("case_sensitive") else contains_any(text, [value])
    return _result(check, hit, f"{path or '$'} {'包含' if hit else '不包含'} {safe_str(value)[:60]!r}")


def check_regex(check: Check, task: TaskLike, output: AgentOutput, ctx: Any) -> CheckResult:
    data = _resolve(check, output, ctx)
    path = check.get("path")
    actual = get_by_path(data, path) if path else data
    pattern = safe_str(check.get("pattern"))
    if not pattern:
        return _result(check, None, "缺少 pattern 参数")
    flags = 0 if check.get("case_sensitive") else re.IGNORECASE
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        return _result(check, None, f"正则非法（{exc.__class__.__name__}）：{pattern[:40]}")
    text = safe_str(actual)
    match = compiled.search(text)
    if match:
        return _result(check, True, f"匹配到 {match.group(0)[:40]!r}")
    return _result(check, False, f"未匹配 /{pattern[:40]}/")


# --------------------------------------------------------------------------- #
# 6. set_includes
# --------------------------------------------------------------------------- #
def check_set_includes(check: Check, task: TaskLike, output: AgentOutput, ctx: Any) -> CheckResult:
    """集合包含：``values`` 中至少 ``min_hits``（默认全部）出现在答案里。"""
    data = _resolve(check, output, ctx)
    path = check.get("path")
    node = get_by_path(data, path) if path else data
    values = [safe_str(v) for v in (check.get("values") or []) if safe_str(v)]
    if not values:
        return _result(check, None, "缺少 values 参数")

    if isinstance(node, (list, tuple)):
        pool = [normalize(v) for v in node]
        hits = [v for v in values if any(normalize(v) in p or p in normalize(v) for p in pool)]
    else:
        text = safe_str(node)
        hits = [v for v in values if contains_any(text, [v])]

    min_hits = int(check.get("min_hits", len(values)) or 0)
    passed = len(hits) >= min_hits
    return _result(
        check,
        passed,
        f"命中 {len(hits)}/{len(values)}（要求 >= {min_hits}）：{'、'.join(hits[:6]) or '无'}",
    )


# --------------------------------------------------------------------------- #
# 7. json_schema（JSON-Schema 子集）
# --------------------------------------------------------------------------- #
def check_json_schema(check: Check, task: TaskLike, output: AgentOutput, ctx: Any) -> CheckResult:
    """结构校验。schema 子集：type / required / properties / items / enum / minimum / maximum。"""
    data = _resolve(check, output, ctx)
    path = check.get("path")
    node = get_by_path(data, path) if path else data
    schema = check.get("schema")
    if node is None:
        return _result(check, False, f"路径 {path or '$'} 未取到值")
    if not isinstance(schema, Mapping):
        return _result(check, None, "缺少 schema 参数")
    problems = validate_json_schema(node, schema, root="$")
    if problems:
        return _result(check, False, "结构不符：" + "；".join(problems[:4]))
    return _result(check, True, "结构校验通过")


_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def validate_json_schema(node: Any, schema: Mapping[str, Any], *, root: str = "$") -> list[str]:
    """极简 JSON-Schema 校验，返回问题列表（空列表 = 通过）。"""
    problems: list[str] = []
    expected_type = schema.get("type")
    if isinstance(expected_type, str):
        allowed = _TYPE_MAP.get(expected_type)
        if allowed is None:
            problems.append(f"{root}: 未知 schema.type={expected_type}")
        else:
            ok = isinstance(node, allowed)
            if expected_type in ("integer", "number") and isinstance(node, bool):
                ok = False
            if not ok:
                problems.append(f"{root}: 期望 {expected_type}")
                return problems

    if "enum" in schema and schema["enum"]:
        allowed_values = schema["enum"]
        if node not in allowed_values:
            problems.append(f"{root}: 取值不在 enum 内")

    if isinstance(node, dict):
        for key in schema.get("required") or []:
            if key not in node:
                problems.append(f"{root}.{key}: 缺少必需字段")
        properties = schema.get("properties") or {}
        if isinstance(properties, Mapping):
            for key in sorted(properties):
                if key in node:
                    problems.extend(
                        validate_json_schema(node[key], properties[key], root=f"{root}.{key}")
                    )
    if isinstance(node, (list, tuple)) and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(node):
            problems.extend(
                validate_json_schema(item, schema["items"], root=f"{root}[{index}]")
            )

    if isinstance(node, (int, float)) and not isinstance(node, bool):
        if "minimum" in schema and node < schema["minimum"]:
            problems.append(f"{root}: 小于 minimum")
        if "maximum" in schema and node > schema["maximum"]:
            problems.append(f"{root}: 大于 maximum")
    if isinstance(node, str):
        # str：按**字符数**判定
        if "minLength" in schema and len(node) < int(schema["minLength"]):
            problems.append(f"{root}: 长度小于 minLength")
        if "maxLength" in schema and len(node) > int(schema["maxLength"]):
            problems.append(f"{root}: 长度大于 maxLength")
    elif isinstance(node, (list, tuple)):
        # array：按**元素个数**判定（JSON-Schema 语义）。
        # 兼容两套关键字：minLength/maxLength（本仓早期约定，见
        # docs/02-任务集规范.md 的 json_schema 示例）与标准的
        # minItems/maxItems。
        length = len(node)
        min_len: int | None = None
        max_len: int | None = None
        if "minLength" in schema:
            min_len = int(schema["minLength"])
        if "minItems" in schema:
            min_len = int(schema["minItems"]) if min_len is None else max(min_len, int(schema["minItems"]))
        if "maxLength" in schema:
            max_len = int(schema["maxLength"])
        if "maxItems" in schema:
            max_len = int(schema["maxItems"]) if max_len is None else min(max_len, int(schema["maxItems"]))
        if min_len is not None and length < min_len:
            problems.append(f"{root}: 元素个数少于 minLength/minItems")
        if max_len is not None and length > max_len:
            problems.append(f"{root}: 元素个数多于 maxLength/maxItems")
    return problems


# --------------------------------------------------------------------------- #
# 8. file_exists
# --------------------------------------------------------------------------- #
def check_file_exists(check: Check, task: TaskLike, output: AgentOutput, ctx: Any) -> CheckResult:
    filename = safe_str(check.get("filename"))
    if not filename:
        return _result(check, None, "缺少 filename 参数")
    if ctx is not None and getattr(ctx, "workspace_dir", None):
        candidate = Path(ctx.workspace_dir) / filename
        if candidate.is_file():
            return _result(check, True, f"产出文件存在：{filename}")
        # 也允许在 output/ 下
        candidate2 = Path(ctx.workspace_dir) / "output" / filename
        if candidate2.is_file():
            return _result(check, True, f"产出文件存在：output/{filename}")
        return _result(check, False, f"产出文件不存在：{filename}")
    present = filename in (output.answer_files or {})
    return _result(check, present, f"产出文件{'存在' if present else '不存在'}：{filename}")


# --------------------------------------------------------------------------- #
# 9. file_json_match
# --------------------------------------------------------------------------- #
def check_file_json_match(check: Check, task: TaskLike, output: AgentOutput, ctx: Any) -> CheckResult:
    """读产出文件中的 JSON，按 ``path`` 取值后与 ``value`` 比较。"""
    filename = safe_str(check.get("filename"))
    if not filename:
        return _result(check, None, "缺少 filename 参数")
    target = _find_produced(output, ctx, filename)
    if target is None:
        return _result(check, False, f"产出文件不存在：{filename}")
    try:
        data = json.loads(target)
    except json.JSONDecodeError:
        return _result(check, False, f"{filename} 不是合法 JSON")
    path = check.get("path")
    actual = get_by_path(data, path) if path else data
    expected = check.get("value")
    op = str(check.get("op", "eq") or "eq").lower()
    tol = float(check.get("tol", _TOL_DEFAULT) or 0.0)

    if isinstance(expected, (int, float)) or _to_number(actual) is not None:
        number = _to_number(actual)
        target_num = _to_number(expected)
        if number is None or target_num is None:
            return _result(check, False, f"{filename}:{path} 取不到数值（实际 {safe_str(actual)[:40]}）")
        if op == "eq":
            passed = approx_equal(number, target_num, tol)
        elif op == "ne":
            passed = not approx_equal(number, target_num, tol)
        elif op in ("gt", "ge", "lt", "le"):
            passed = _OPS[op](number, target_num)
        else:
            return _result(check, None, f"不支持的 op：{op}")
        return _result(check, passed, f"{filename}:{path} = {number:g}（期望 {op} {target_num:g}）")

    passed = safe_str(actual) == safe_str(expected)
    return _result(check, passed, f"{filename}:{path} = {safe_str(actual)[:40]!r}")


def _find_produced(output: AgentOutput, ctx: Any, filename: str) -> str | None:
    """在 workspace / output 中按文件名查找产出文件内容。"""
    if ".." in Path(filename).parts:
        return None
    bases: list[Path] = []
    if ctx is not None and getattr(ctx, "workspace_dir", None):
        bases.append(Path(ctx.workspace_dir))
    for base in bases:
        for candidate in (base / filename, base / "output" / filename):
            if candidate.is_file():
                try:
                    return candidate.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    return None
    rel = (output.answer_files or {}).get(filename)
    if rel:
        return _read_text_rel(output, rel)
    # 允许用通配找同名
    found = _first_file(output, [re.escape(Path(filename).name)])
    return found[1] if found else None


# --------------------------------------------------------------------------- #
# 10. year_tag
# --------------------------------------------------------------------------- #
def check_year_tag(check: Check, task: TaskLike, output: AgentOutput, ctx: Any) -> CheckResult:
    """年份标识是否显式标注（政策题关键）。"""
    year = check.get("year")
    require_explicit = bool(check.get("require_explicit", True))
    text = _point_haystack(output)
    year_str = safe_str(year)
    if not year_str:
        return _result(check, None, "缺少 year 参数")
    found = year_str in text
    if found:
        return _result(check, True, f"答案显式出现年份 {year_str}")
    if not require_explicit:
        return _result(check, None, f"未显式标注年份 {year_str}，且 require_explicit=false")
    return _result(check, False, f"答案未显式标注年份 {year_str}")


# --------------------------------------------------------------------------- #
# 11/12. source_domain / source_level
# --------------------------------------------------------------------------- #
def check_source_domain(check: Check, task: TaskLike, output: AgentOutput, ctx: Any) -> CheckResult:
    suffixes = [str(x) for x in (check.get("domain_suffix") or [])]
    min_count = int(check.get("min_count", 1) or 1)
    sources = _sources_of(output, ctx, task)
    if not suffixes:
        return _result(check, None, "缺少 domain_suffix 参数")
    matched = [s for s in sources if domain_matches(s.domain, suffixes)]
    passed = len(matched) >= min_count
    return _result(
        check,
        passed,
        f"官方域名来源 {len(matched)} 条（要求 >= {min_count}）："
        + "、".join(safe_str(s.domain or s.url)[:40] for s in matched[:3])
        if matched
        else f"无匹配 {'/'.join(suffixes)} 的来源（要求 >= {min_count}）",
    )


def check_source_level(check: Check, task: TaskLike, output: AgentOutput, ctx: Any) -> CheckResult:
    level_min = safe_str(check.get("level_min")) or "E4"
    min_count = int(check.get("min_count", 1) or 1)
    sources = _sources_of(output, ctx, task)
    if not sources:
        return _result(check, False, "无任何来源，无法满足证据等级要求")
    hits = count_at_least(sources, level_min)
    passed = hits >= min_count
    levels = "、".join(sorted({s.evidence_level for s in sources}))
    return _result(
        check,
        passed,
        f"等级 >= {level_min} 的来源 {hits} 条（要求 >= {min_count}）；实际等级集合：{levels}",
    )


# --------------------------------------------------------------------------- #
# 13. citation_coverage
# --------------------------------------------------------------------------- #
def check_citation_coverage(check: Check, task: TaskLike, output: AgentOutput, ctx: Any) -> CheckResult:
    """引用覆盖与准确率。

    ``min_citations``：引用总数下限；``min_supported_ratio``：已判定引用的支持率下限。
    引用完全未判定（``supported is None``）时按「无法判定」处理（不假装通过）。
    """
    citations = list(output.citations or [])
    min_citations = int(check.get("min_citations", 1) or 0)
    min_ratio = float(check.get("min_supported_ratio", 0.0) or 0.0)

    if not citations:
        if min_citations > 0:
            return _result(check, False, f"无任何引用（要求 >= {min_citations} 条）")
        return _result(check, None, "无引用且未要求引用，无法判定")

    judged = [c for c in citations if c.supported is not None]
    if not judged:
        return _result(
            check,
            None,
            f"引用 {len(citations)} 条但全部未判定（supported=null），无法判定准确率",
        )
    supported = sum(1 for c in judged if c.supported)
    ratio = supported / len(judged)
    count_ok = len(citations) >= min_citations
    ratio_ok = ratio >= min_ratio
    passed = count_ok and ratio_ok
    return _result(
        check,
        passed,
        f"引用 {len(citations)} 条（要求 >= {min_citations}），"
        f"已判定 {len(judged)} 条支持率 {ratio:.2%}（要求 >= {min_ratio:.0%}）",
    )


# --------------------------------------------------------------------------- #
# 14. must_not_claim（命中即幻觉违规）
# --------------------------------------------------------------------------- #
def check_must_not_claim(check: Check, task: TaskLike, output: AgentOutput, ctx: Any) -> CheckResult:
    """命中 ``patterns`` 即幻觉违规。**critical 强制为 True**。"""
    check.critical = True
    patterns = [safe_str(p) for p in (check.get("patterns") or task.expected.must_not_claim or [])]
    patterns = [p for p in patterns if p]
    if not patterns:
        return _result(check, None, "无 must_not_claim 模式，无法判定")
    text = _point_haystack(output)
    hits = [p for p in patterns if contains_any(text, [p])]
    if hits:
        return _result(check, False, f"⚠ 幻觉违规：命中禁止断言 {'、'.join(hits[:3])}")
    return _result(check, True, f"未命中任何禁止断言（共检查 {len(patterns)} 条）")


# --------------------------------------------------------------------------- #
# 15. constraint（规划/排程约束）
# --------------------------------------------------------------------------- #
class ConstraintEvaluator:
    """评估 ``constraint.rules``（方案 5.3 的 PLAN-002 例）。

    支持 kind：
      - ``daily_total_hours``：``{op, value}`` 每日总时长
      - ``subject_min_hours``：``{subject, op, value}`` 某科目每日/总时长
      - ``weekly_rest``：``{op, value}`` 每周休息天数
      - ``total_days``：``{op, value}`` 总天数
      - ``no_overlap``：``{value: true}`` 同一天内时段不重叠
      - ``field``：通用字段断言 ``{field, op, value}``
    """

    OPS: dict[str, Callable[[float, float], bool]] = _OPS

    def __init__(self, schedule: Any) -> None:
        self.schedule = schedule
        self.days = self._extract_days(schedule)

    # -- 结构提取 ----------------------------------------------------------
    @staticmethod
    def _extract_days(schedule: Any) -> list[Mapping[str, Any]]:
        if isinstance(schedule, Mapping):
            for key in ("schedule", "days", "plan", "items", "timetable"):
                value = schedule.get(key)
                if isinstance(value, list):
                    return [d for d in value if isinstance(d, Mapping)]
            return []
        if isinstance(schedule, list):
            return [d for d in schedule if isinstance(d, Mapping)]
        return []

    @staticmethod
    def _slots(day: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        for key in ("slots", "sessions", "tasks", "items", "subjects", "blocks"):
            value = day.get(key)
            if isinstance(value, list):
                return [s for s in value if isinstance(s, Mapping)]
        return []

    def _slot_field(self, slot: Mapping[str, Any], *names: str) -> Any:
        for name in names:
            if name in slot:
                return slot[name]
        return None

    def _slot_hours(self, slot: Mapping[str, Any]) -> float:
        hours = self._slot_field(slot, "hours", "duration_hours", "h", "duration")
        value = _to_number(hours)
        if value is not None:
            return value
        start = self._slot_field(slot, "start", "start_time", "from")
        end = self._slot_field(slot, "end", "end_time", "to")
        return _hours_between(start, end)

    def _slot_subject(self, slot: Mapping[str, Any]) -> str:
        return normalize(self._slot_field(slot, "subject", "name", "course", "topic"))

    # -- 规则 --------------------------------------------------------------
    def evaluate(self, rules: Sequence[Mapping[str, Any]]) -> tuple[bool, list[str]]:
        details: list[str] = []
        passed = True
        for index, rule in enumerate(rules):
            if not isinstance(rule, Mapping):
                continue
            kind = safe_str(rule.get("kind")) or f"rule{index}"
            ok, detail = self._apply(kind, rule)
            details.append(f"{kind}: {'OK' if ok else 'FAIL'} — {detail}")
            passed = passed and ok
        return passed, details

    def _apply(self, kind: str, rule: Mapping[str, Any]) -> tuple[bool, str]:
        op = safe_str(rule.get("op")) or "eq"
        expected = _to_number(rule.get("value"))
        if kind == "total_days":
            actual = float(len(self.days))
        elif kind == "weekly_rest":
            rest_days = sum(1 for d in self.days if self._is_rest_day(d))
            weeks = max(1.0, len(self.days) / 7.0)
            actual = rest_days / weeks
        elif kind == "daily_total_hours":
            totals = [self._day_total(d) for d in self.days]
            if not totals:
                return False, "日程为空，无法计算每日总时长"
            actual = totals[0]
            if expected is not None and any(not self.OPS.get(op, lambda a, b: False)(t, expected) for t in totals):
                bad = [round(t, 3) for t in totals if not self.OPS.get(op, lambda a, b: False)(t, expected)]
                return False, f"存在不规范天数：{bad[:5]}"
        elif kind == "subject_min_hours":
            subject = normalize(rule.get("subject"))
            totals = [
                sum(self._slot_hours(s) for s in self._slots(d) if subject in self._slot_subject(s))
                for d in self.days
            ]
            actual = min(totals) if totals else 0.0
        elif kind == "no_overlap":
            want = bool(rule.get("value", True))
            overlaps = self._find_overlaps()
            ok = (not overlaps) if want else bool(overlaps)
            return ok, ("无时段重叠" if not overlaps else f"发现重叠：{overlaps[:3]}")
        elif kind == "field":
            field = safe_str(rule.get("field"))
            actual = _to_number(_dig(self.schedule, field))
            if actual is None:
                return False, f"字段 {field} 取不到数值"
        elif kind == "assigns_day":
            return self._check_assigns_day(rule)
        else:
            return False, f"不支持的 constraint kind：{kind}"

        if actual is None:
            return False, "无法计算实际值"
        if expected is None:
            return False, "规则缺少 value"
        fn = self.OPS.get(op)
        if fn is None:
            return False, f"不支持的 op：{op}"
        return fn(float(actual), expected), f"实际 {actual:g}，期望 {op} {expected:g}"

    def _check_assigns_day(self, rule: Mapping[str, Any]) -> tuple[bool, str]:
        subject = normalize(rule.get("subject"))
        day_label = safe_str(rule.get("value"))
        for day in self.days:
            label = safe_str(day.get("date") or day.get("day") or day.get("label"))
            if label == day_label:
                has = any(subject in self._slot_subject(s) for s in self._slots(day))
                return has, f"第 {day_label} 天{'包含' if has else '不含'} {subject}"
        return False, f"找不到第 {day_label} 天"

    # -- 辅助 --------------------------------------------------------------
    def _is_rest_day(self, day: Mapping[str, Any]) -> bool:
        flag = day.get("rest") or day.get("is_rest_day")
        if isinstance(flag, bool) and flag:
            return True
        total = self._day_total(day)
        return total <= 0.0

    def _day_total(self, day: Mapping[str, Any]) -> float:
        direct = _to_number(self._slot_field(day, "total_hours", "hours", "total"))
        if direct is not None:
            return direct
        return sum(self._slot_hours(s) for s in self._slots(day))

    def _find_overlaps(self) -> list[str]:
        found: list[str] = []
        for day in self.days:
            label = safe_str(day.get("date") or day.get("day") or day.get("label") or "?")
            spans: list[tuple[float, float]] = []
            for slot in self._slots(day):
                start = _to_minutes(self._slot_field(slot, "start", "start_time", "from"))
                end = _to_minutes(self._slot_field(slot, "end", "end_time", "to"))
                if start is None or end is None:
                    continue
                if end <= start:
                    found.append(f"{label} 时段非法（end <= start）")
                spans.append((start, end))
            spans.sort()
            for i in range(len(spans) - 1):
                if spans[i + 1][0] < spans[i][1]:
                    found.append(f"{label} 时段重叠")
        return found


def _dig(data: Any, path: str) -> Any:
    if not path:
        return data
    return get_by_path(data, path)


def _hours_between(start: Any, end: Any) -> float:
    s = _to_minutes(start)
    e = _to_minutes(end)
    if s is None or e is None or e < s:
        return 0.0
    return (e - s) / 60.0


def _to_minutes(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # 数字按小时处理（便于 "start: 8, end: 10" 的简写）
        return float(value) * 60.0
    text = safe_str(value).strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            hours = int(parts[0])
            minutes = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            return None
        return float(hours * 60 + minutes)
    number = _to_number(text)
    return number * 60.0 if number is not None else None


def check_constraint(check: Check, task: TaskLike, output: AgentOutput, ctx: Any) -> CheckResult:
    data = _resolve(check, output, ctx)
    answer_path = check.get("answer_path")
    schedule = get_by_path(data, answer_path) if answer_path else data
    if schedule is None:
        return _result(check, False, f"路径 {answer_path or '$'} 未取到排程数据")
    rules = check.get("rules") or []
    if not rules:
        return _result(check, None, "缺少 rules 参数")
    evaluator = ConstraintEvaluator(schedule)
    passed, details = evaluator.evaluate(rules)
    return _result(check, passed, " | ".join(details))


# --------------------------------------------------------------------------- #
# 16. evidence_level
# --------------------------------------------------------------------------- #
def check_evidence_level(check: Check, task: TaskLike, output: AgentOutput, ctx: Any) -> CheckResult:
    """断言是否带 E 级标注（``{path, expected_min}``）。"""
    data = _resolve(check, output, ctx)
    path = check.get("path")
    node = get_by_path(data, path) if path else data
    expected_min = safe_str(check.get("expected_min")) or "E3"
    if node is None:
        return _result(check, False, f"路径 {path or '$'} 未取到 E 级标注")

    levels = _collect_levels(node)
    if not levels:
        return _result(check, False, f"路径 {path or '$'} 中未找到任何 E0~E5 标注")
    best = max(levels, key=level_rank)
    passed = level_rank(best) >= level_rank(expected_min)
    return _result(
        check,
        passed,
        f"标注等级 {best}（要求 >= {expected_min}）；全部标注：{'、'.join(sorted(set(levels)))}",
    )


def _collect_levels(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, str):
        for match in re.findall(r"\bE[0-5]\b", node):
            found.append(match)
    elif isinstance(node, Mapping):
        for key in sorted(node):
            value = node[key]
            if str(key).lower() in ("evidence_level", "level") and isinstance(value, str):
                if re.fullmatch(r"E[0-5]", value.strip()):
                    found.append(value.strip())
            else:
                found.extend(_collect_levels(value))
    elif isinstance(node, (list, tuple)):
        for item in node:
            found.extend(_collect_levels(item))
    return found


# --------------------------------------------------------------------------- #
# 分发
# --------------------------------------------------------------------------- #
def _resolve(check: Check, output: AgentOutput, ctx: Any) -> Any:
    """解析 ``path`` 的根数据源：默认 answer payload，支持 ``$.sources`` / ``$.citations``。"""
    path = safe_str(check.get("path"))
    if path.startswith("$.sources"):
        return {"sources": [s.to_dict() for s in output.sources], "data": [s.to_dict() for s in output.sources]}
    if path.startswith("$.citations"):
        return {"citations": [c.to_dict() for c in output.citations]}
    if path.startswith("$.search_trace"):
        return {"search_trace": output.search_trace or {}}
    return answer_payload(output)


HANDLERS: dict[str, Callable[[Check, Any, AgentOutput, Any], CheckResult]] = {
    "point_hit": check_point_hit,
    "numeric": check_numeric,
    "string_eq": check_string_eq,
    "string_contains": check_string_contains,
    "regex": check_regex,
    "set_includes": check_set_includes,
    "json_schema": check_json_schema,
    "file_exists": check_file_exists,
    "file_json_match": check_file_json_match,
    "year_tag": check_year_tag,
    "source_domain": check_source_domain,
    "source_level": check_source_level,
    "citation_coverage": check_citation_coverage,
    "must_not_claim": check_must_not_claim,
    "constraint": check_constraint,
    "evidence_level": check_evidence_level,
}


def run_check(check: Check, task: Any, output: AgentOutput, ctx: Any) -> CheckResult:
    """单个声明式检查的执行入口（方案 5.3）。**绝不抛异常**，异常转为 ``passed=None``。"""
    _workspace_dir(output, ctx)
    handler = HANDLERS.get(check.type)
    if handler is None:
        return _result(check, None, f"未知 check 类型：{check.type}")
    try:
        result = handler(check, task, output, ctx)
    except GraderError:
        raise
    except Exception as exc:  # noqa: BLE001 - 单个 check 崩溃不应拖垮整轮评分
        return _result(check, None, f"check 执行异常（{type(exc).__name__}）：{safe_str(exc)[:80]}")
    if check.type == "must_not_claim":
        result.critical = True
    return result


# 类型别名：checks 不直接依赖 Task，仅用其 expected 属性，便于单测注入简易对象
TaskLike = Any
