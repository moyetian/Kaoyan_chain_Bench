"""文本归一化与匹配工具（零依赖，供 checks / graders 使用）。

设计原则：
- **纯函数**：不依赖时间、随机、字典遍历顺序。
- 归一化区分「宽松」（大小写/全角/空白/标点）与「严格」两级，check 可按需选择。
- 关键词匹配默认用「归一化子串」，因为中文没有空格分词，子串匹配最稳。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "normalize",
    "normalize_loose",
    "tokenize",
    "contains_any",
    "contains_all",
    "count_hits",
    "any_of_hit",
    "all_of_hit",
    "extract_numbers",
    "approx_equal",
    "truncate",
    "safe_str",
    "dedup_keep_order",
    "coverage_ratio",
    "looks_like_json",
    "html_to_text",
]

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(
    r"[\u3000-\u303f\uff00-\uffef!-/:-@\[-`{-~、。，；：？！“”‘’（）《》【】—…·]+"
)
_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")

#: 全角 → 半角映射范围（ASCII 可见字符的偏移）。
_FULLWIDTH_OFFSET = 0xFEE0


def safe_str(value: Any) -> str:
    """把任意值安全转成 str；``None`` → ``''``。不抛异常。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # 避免 0.30000000000000004 之类的浮点噪声影响展示
        text = f"{value:.10f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def normalize(text: Any) -> str:
    """宽松归一化：NFKC + 全角转半角 + 小写 + 去除全部空白 + 去除标点。

    用于中文关键词匹配（``"初试科目：" == "初试科目"``）。
    """
    raw = safe_str(text)
    if not raw:
        return ""
    raw = unicodedata.normalize("NFKC", raw)
    out_chars: list[str] = []
    for ch in raw:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out_chars.append(chr(code - _FULLWIDTH_OFFSET))
        else:
            out_chars.append(ch)
    raw = "".join(out_chars).lower()
    raw = _WS_RE.sub("", raw)
    raw = _PUNCT_RE.sub("", raw)
    return raw


def normalize_loose(text: Any) -> str:
    """更宽松：只做 NFKC + 小写 + 折叠空白，保留标点（用于 prose 相似度）。"""
    raw = unicodedata.normalize("NFKC", safe_str(text)).lower()
    return _WS_RE.sub(" ", raw).strip()


def tokenize(text: Any) -> list[str]:
    """中英混合粗分词：中文按字符（二元组）+ 英文按 ``\\w+``。

    不追求语言学正确，只用于覆盖率统计等启发式场景。
    """
    raw = normalize_loose(text)
    tokens: list[str] = []
    for word in re.findall(r"[a-z0-9_\u4e00-\u9fff]+", raw):
        if re.fullmatch(r"[a-z0-9_]+", word):
            tokens.append(word)
        else:
            if len(word) == 1:
                tokens.append(word)
            else:
                tokens.extend(word[i : i + 2] for i in range(len(word) - 1))
    return tokens


def contains_any(haystack: Any, needles: Iterable[Any], *, strict: bool = False) -> bool:
    """任一 needle 命中即 True（归一化子串匹配）。空 needles → False。"""
    text = safe_str(haystack) if strict else normalize(haystack)
    items = [safe_str(n) if strict else normalize(n) for n in needles]
    items = [n for n in items if n]
    if not items:
        return False
    return any(n in text for n in items)


def contains_all(haystack: Any, needles: Iterable[Any], *, strict: bool = False) -> bool:
    """全部 needle 命中才 True。空 needles → False（无要求即不通过，避免假阳性）。"""
    text = safe_str(haystack) if strict else normalize(haystack)
    items = [safe_str(n) if strict else normalize(n) for n in needles]
    items = [n for n in items if n]
    if not items:
        return False
    return all(n in text for n in items)


def count_hits(haystack: Any, needles: Iterable[Any], *, strict: bool = False) -> int:
    text = safe_str(haystack) if strict else normalize(haystack)
    items = [safe_str(n) if strict else normalize(n) for n in needles]
    return sum(1 for n in items if n and n in text)


def any_of_hit(text: Any, any_of: Sequence[Any], all_of: Sequence[Any] = (), **kw: Any) -> bool:
    """``any_of`` 命中任一 **且** ``all_of`` 全部命中。两组都为空 → False。"""
    a = list(any_of or [])
    b = list(all_of or [])
    if a and not contains_any(text, a, **kw):
        return False
    if b and not contains_all(text, b, **kw):
        return False
    return bool(a or b)


def all_of_hit(text: Any, all_of: Sequence[Any], **kw: Any) -> bool:
    return contains_all(text, all_of, **kw)


def extract_numbers(text: Any) -> list[float]:
    """抽取文本中全部数字（含小数、负号），按出现顺序返回。"""
    out: list[float] = []
    for raw in _NUM_RE.findall(safe_str(text)):
        try:
            out.append(float(raw))
        except ValueError:  # pragma: no cover - 正则已保证
            continue
    return out


def approx_equal(a: Any, b: Any, tol: float = 1e-9) -> bool:
    """数值近似相等：支持 int/float/可转数字的字符串；非数字退化为字符串比较。"""
    try:
        fa = float(a)
        fb = float(b)
    except (TypeError, ValueError):
        return safe_str(a) == safe_str(b)
    if tol < 0:
        tol = -tol
    return abs(fa - fb) <= tol


def truncate(text: Any, limit: int = 4000) -> tuple[str, bool]:
    """截断文本到 ``limit`` 字符，返回 ``(文本, 是否被截断)``。"""
    raw = safe_str(text)
    if limit < 0:
        limit = 0
    if len(raw) <= limit:
        return raw, False
    return raw[:limit], True


def dedup_keep_order(items: Iterable[Any]) -> list[Any]:
    """去重并保持首次出现顺序（不依赖集合遍历顺序）。"""
    seen: set[Any] = set()
    out: list[Any] = []
    for item in items:
        key = item if isinstance(item, (str, int, float, bool, type(None))) else repr(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def coverage_ratio(text: Any, required: Iterable[Any], **kw: Any) -> float:
    """必含项覆盖率（0.0~1.0）。``required`` 为空 → 返回 ``0.0``。"""
    items = [x for x in required if safe_str(x)]
    if not items:
        return 0.0
    return count_hits(text, items, **kw) / len(items)


def looks_like_json(text: Any) -> bool:
    raw = safe_str(text).strip()
    if len(raw) < 2:
        return False
    if raw[0] not in "[{":
        return False
    return raw[-1] in "]}"


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


def html_to_text(html: Any) -> str:
    """极简 HTML → 文本（去脚本样式、去标签、解常见实体）。仅用于启发式比较。"""
    raw = safe_str(html)
    if not raw:
        return ""
    raw = _SCRIPT_RE.sub(" ", raw)
    raw = re.sub(r"<!--.*?-->", " ", raw, flags=re.DOTALL)
    raw = _TAG_RE.sub(" ", raw)
    replacements = {
        "&nbsp;": " ",
        "&amp;": "&",
        "&lt;": "<",
        "&gt;": ">",
        "&quot;": '"',
        "&#39;": "'",
    }
    for key, value in replacements.items():
        raw = raw.replace(key, value)
    return _WS_RE.sub(" ", raw).strip()


def to_float_or_none(value: Any) -> float | None:
    """尽力转 float，失败返回 ``None``（**不用 0 冒充**）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    num = to_float_or_none(value)
    return int(num) if num is not None else None


def clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def get_by_path(data: Any, path: str | None, default: Any = None) -> Any:
    """极简 JSONPath 子集：``$.a.b[0].c`` / ``a.b`` / ``$``。

    支持：根 ``$``、点号路径、数组下标 ``[n]``。不支持通配 ``*`` / 递归 ``..``。
    路径为空 → 返回 ``data`` 本身。
    """
    if path is None or path in ("", "$"):
        return data
    expr = path.strip()
    if expr.startswith("$"):
        expr = expr[1:]
    if expr.startswith("."):
        expr = expr[1:]
    if not expr:
        return data
    node = data
    for raw_part in _split_path(expr):
        part = raw_part.strip()
        if not part:
            continue
        if part.startswith("[") and part.endswith("]"):
            idx_raw = part[1:-1].strip().strip("'\"")
            if idx_raw == "*":
                return default
            try:
                idx = int(idx_raw)
            except ValueError:
                return default
            if not isinstance(node, (list, tuple)) or not (-len(node) <= idx < len(node)):
                return default
            node = node[idx]
            continue
        if isinstance(node, Mapping):
            if part not in node:
                return default
            node = node[part]
        elif isinstance(node, (list, tuple)):
            # 允许 list 上的数字键（如 "0"）
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return node


def _split_path(expr: str) -> list[str]:
    """把 ``a.b[0].c`` 拆成 ``['a', 'b', '[0]', 'c']``。"""
    parts: list[str] = []
    buf: list[str] = []
    for ch in expr:
        if ch == ".":
            if buf:
                parts.append("".join(buf))
                buf = []
        elif ch == "[":
            if buf:
                parts.append("".join(buf))
                buf = []
            buf.append(ch)
        else:
            buf.append(ch)
            if ch == "]":
                parts.append("".join(buf))
                buf = []
    if buf:
        parts.append("".join(buf))
    return parts
