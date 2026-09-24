"""受限 YAML 子集解析器（零依赖，对应方案 C-03 / 5.11）。

支持范围（**只允许这些**，其余一律报错并给出行号）：
  - ``#`` 注释（行首或值后，双引号内的 ``#`` 不算注释）
  - ``key: value`` 映射，任意层级缩进（推荐 2 空格，但按实际缩进层级解析）
  - 标量：``str`` / ``int`` / ``float`` / ``bool``(true/false/yes/no) / ``null``(null/~/空)
  - 单/双引号字符串（双引号支持 ``\\n \\t \\" \\\\`` 转义）
  - 块序列 ``- item``，支持「标量元素」与「行内映射元素」（``- name: x``）
  - 行内紧凑写法：``{k: v, k2: v2}`` 与 ``[a, b, c]``

不支持（会抛 :class:`~kaoyanbench.core.errors.ConfigError` 并附行号）：
  - 锚点/别名 ``&`` ``*``、标签 ``!``
  - 多行块标量 ``|`` ``>``
  - 多文档分隔符 ``---`` / ``...``
  - 制表符缩进

注意：本模块属于 ``kaoyanbench/utils``，**不得**依赖 ``core`` 里的实现细节；
仅导入 ``core.errors.ConfigError`` 以满足方案 5.11 要求的异常类型。
"""

from __future__ import annotations

import re
from typing import Any

from ..core.errors import ConfigError

__all__ = ["loads", "load", "MiniYamlError", "SUPPORTED_HINT"]

#: 报错时附带的安装建议（方案 5.11 原文要求）。
SUPPORTED_HINT = "请安装 pyyaml：pip install pyyaml"

_BOOL_TRUE = {"true", "yes", "on"}
_BOOL_FALSE = {"false", "no", "off"}
_NULL_LITERALS = {"null", "~", "none", ""}
_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
# 允许 ``key: value``、``key:value``（仅在后面紧跟流样式 ``{``/``[`` 时才允许无空格，
# 避免把 URL 里的 ``http://`` 误判为 key）以及 ``key:``（值在下一行块中）。
_KEY_RE = re.compile(r"^([^:#\s][^:#]*?)\s*:(?:\s+(.*)|(?=[{\[])(.*))?$")


class MiniYamlError(ConfigError):
    """受限 YAML 解析失败。``ConfigError`` 子类，携带行号。"""

    def __init__(self, message: str, line_no: int | None = None, line_text: str = "") -> None:
        detail = f"不支持的 YAML 语法（第 {line_no} 行）：{message}。{SUPPORTED_HINT}"
        super().__init__(detail, context={"line": line_no, "text": line_text})
        self.line_no = line_no
        self.line_text = line_text


# --------------------------------------------------------------------------- #
# 预处理
# --------------------------------------------------------------------------- #
def _strip_comment(line: str) -> str:
    """去掉行尾注释，尊重单/双引号内的 ``#``。"""
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        else:
            if ch in ("'", '"'):
                quote = ch
                out.append(ch)
            elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
                break
            else:
                out.append(ch)
        i += 1
    return "".join(out)


class _Line:
    __slots__ = ("indent", "text", "no")

    def __init__(self, indent: int, text: str, no: int) -> None:
        self.indent = indent
        self.text = text
        self.no = no


def _prepare(text: str) -> list[_Line]:
    """把原始文本变成 (indent, text, 行号) 列表，剔除空行与注释行。"""
    lines: list[_Line] = []
    for idx, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw.split("#")[0][: len(raw.split("#")[0])]:
            # 制表符缩进不受支持（YAML 规范亦禁止）
            stripped = raw.lstrip(" \t")
            if raw[: len(raw) - len(stripped)].count("\t"):
                raise MiniYamlError("制表符缩进不受支持，请改用空格", idx, raw)
        body = raw.rstrip()
        stripped_raw = body.lstrip(" ")
        if stripped_raw.startswith("---") or stripped_raw.startswith("..."):
            raise MiniYamlError("不支持多文档分隔符 '---' / '...'", idx, raw)
        body = _strip_comment(body).rstrip()
        if not body.strip():
            continue
        indent = len(body) - len(body.lstrip(" "))
        lines.append(_Line(indent, body.strip(), idx))
    return lines


# --------------------------------------------------------------------------- #
# 标量
# --------------------------------------------------------------------------- #
def _parse_scalar(token: str, line_no: int) -> Any:
    token = token.strip()
    if token == "":
        return None
    first = token[0]
    if first in "&*!":
        raise MiniYamlError(f"不支持锚点/别名/标签语法（'{first}'）", line_no, token)
    if first in "|>":
        raise MiniYamlError("不支持多行块标量 '|' / '>'", line_no, token)
    if first == "{":
        return _parse_flow_map(token, line_no)
    if first == "[":
        return _parse_flow_seq(token, line_no)
    if first in ("'", '"'):
        return _parse_quoted(token, line_no)
    return _parse_plain(token)


def _parse_quoted(token: str, line_no: int) -> str:
    quote = token[0]
    if len(token) < 2 or not token.endswith(quote):
        raise MiniYamlError("引号字符串未闭合", line_no, token)
    inner = token[1:-1]
    if quote == "'":
        return inner.replace("''", "'")
    return _unescape_double(inner, line_no, token)


def _unescape_double(inner: str, line_no: int, token: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(inner):
            raise MiniYamlError("双引号字符串以反斜杠结尾", line_no, token)
        nxt = inner[i + 1]
        mapping = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "r": "\r", "0": "\0"}
        if nxt not in mapping:
            raise MiniYamlError(f"不支持的双引号转义 '\\{nxt}'", line_no, token)
        out.append(mapping[nxt])
        i += 2
    return "".join(out)


def _parse_plain(token: str) -> Any:
    upper = token.lower()
    if upper in _BOOL_TRUE:
        return True
    if upper in _BOOL_FALSE:
        return False
    if upper in _NULL_LITERALS:
        return None
    if _INT_RE.match(token):
        return int(token)
    if _FLOAT_RE.match(token):
        return float(token)
    return token


# --------------------------------------------------------------------------- #
# 行内流样式 {..} / [..]（紧凑写法）
# --------------------------------------------------------------------------- #
def _split_flow(body: str, line_no: int, token: str) -> list[str]:
    """按顶层逗号切分流样式内容，尊重嵌套括号与引号。"""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    buf: list[str] = []
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch in "{[":
            depth += 1
            buf.append(ch)
        elif ch in "}]":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if quote:
        raise MiniYamlError("流样式中的引号未闭合", line_no, token)
    if depth != 0:
        raise MiniYamlError("流样式括号不配对", line_no, token)
    tail = "".join(buf)
    if tail.strip():
        parts.append(tail)
    return [p for p in (p.strip() for p in parts) if p != ""]


def _parse_flow_map(token: str, line_no: int) -> dict[str, Any]:
    if not token.endswith("}"):
        raise MiniYamlError("流样式映射未以 '}' 闭合", line_no, token)
    body = token[1:-1].strip()
    result: dict[str, Any] = {}
    if not body:
        return result
    for item in _split_flow(body, line_no, token):
        if ":" not in item:
            raise MiniYamlError("流样式映射元素缺少 ':'", line_no, item)
        key_raw, _, value_raw = item.partition(":")
        key = _parse_key(key_raw.strip(), line_no, item)
        result[key] = _parse_scalar(value_raw.strip(), line_no)
    return result


def _parse_flow_seq(token: str, line_no: int) -> list[Any]:
    if not token.endswith("]"):
        raise MiniYamlError("流样式序列未以 ']' 闭合", line_no, token)
    body = token[1:-1].strip()
    if not body:
        return []
    return [_parse_scalar(item, line_no) for item in _split_flow(body, line_no, token)]


def _parse_key(raw: str, line_no: int, token: str) -> str:
    if not raw:
        raise MiniYamlError("键名为空", line_no, token)
    if raw[0] in "&*!":
        raise MiniYamlError("不支持锚点/别名/标签作为键", line_no, token)
    if raw[0] in ("'", '"'):
        return str(_parse_quoted(raw, line_no))
    return raw


# --------------------------------------------------------------------------- #
# 递归下降
# --------------------------------------------------------------------------- #
class _Parser:
    def __init__(self, lines: list[_Line]) -> None:
        self.lines = lines
        self.pos = 0

    # -- 工具 --------------------------------------------------------------
    def _peek(self) -> _Line | None:
        return self.lines[self.pos] if self.pos < len(self.lines) else None

    def _is_seq_item(self, line: _Line) -> bool:
        return line.text == "-" or line.text.startswith("- ")

    # -- 主入口 ------------------------------------------------------------
    def parse(self) -> Any:
        if not self.lines:
            return {}
        first = self.lines[0]
        if first.indent != 0:
            raise MiniYamlError("顶层元素不得缩进", first.no, first.text)
        value = self._parse_block(0)
        leftover = self._peek()
        if leftover is not None:
            raise MiniYamlError("缩进层级不一致，无法继续解析", leftover.no, leftover.text)
        return value

    def _parse_block(self, indent: int) -> Any:
        line = self._peek()
        if line is None:
            return None
        if self._is_seq_item(line):
            return self._parse_seq(indent)
        return self._parse_map(indent)

    # -- 序列 --------------------------------------------------------------
    def _parse_seq(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while True:
            line = self._peek()
            if line is None or line.indent < indent:
                break
            if line.indent > indent:
                raise MiniYamlError("序列元素缩进过深", line.no, line.text)
            if not self._is_seq_item(line):
                break
            body = line.text[1:].strip()
            self.pos += 1
            if body == "":
                nxt = self._peek()
                if nxt is not None and nxt.indent > indent:
                    items.append(self._parse_block(nxt.indent))
                else:
                    items.append(None)
                continue
            if ":" in body and not body.startswith(("{", "[", "'", '"')):
                items.append(self._parse_inline_map_item(body, indent, line))
                continue
            items.append(_parse_scalar(body, line.no))
        return items

    def _parse_inline_map_item(self, body: str, indent: int, line: _Line) -> dict[str, Any]:
        """处理 ``- name: x`` 形式：先解析该键值，再把同缩进的后续键并入。"""
        key_raw, _, value_raw = body.partition(":")
        key = _parse_key(key_raw.strip(), line.no, body)
        item: dict[str, Any] = {}
        nested_indent = indent + 2
        value_str = value_raw.strip()
        if value_str:
            item[key] = _parse_scalar(value_str, line.no)
            nested_indent = indent + 2
        else:
            nxt = self._peek()
            if nxt is not None and nxt.indent > indent:
                item[key] = self._parse_block(nxt.indent)
                nested_indent = nxt.indent
            else:
                item[key] = None
        # 继续吸收属于同一个 map 元素的后续键（缩进 > 序列层，且不是新的 '- '）
        while True:
            nxt = self._peek()
            if nxt is None or nxt.indent <= indent or self._is_seq_item(nxt):
                break
            if nxt.indent != nested_indent:
                raise MiniYamlError("映射元素缩进层级不一致", nxt.no, nxt.text)
            sub_key, sub_value = self._parse_pair(nxt, nested_indent)
            item[sub_key] = sub_value
        return item

    # -- 映射 --------------------------------------------------------------
    def _parse_map(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while True:
            line = self._peek()
            if line is None or line.indent < indent:
                break
            if line.indent > indent:
                raise MiniYamlError("缩进层级不一致（期望 %d 个空格）" % indent, line.no, line.text)
            if self._is_seq_item(line):
                break
            key, value = self._parse_pair(line, indent)
            if key in result:
                raise MiniYamlError(f"重复的键 '{key}'", line.no, line.text)
            result[key] = value
        return result

    def _parse_pair(self, line: _Line, indent: int) -> tuple[str, Any]:
        stripped = line.text
        if ":" not in stripped:
            raise MiniYamlError("缺少 ':'，不是合法的 key: value 行", line.no, line.text)
        if stripped[0] in "{[":  # 顶层流样式
            value = _parse_scalar(stripped, line.no)
            self.pos += 1
            if not isinstance(value, dict):
                raise MiniYamlError("顶层流样式必须是映射", line.no, line.text)
            # 顶层流映射：展开成当前层级内容
            self.pos -= 1
            self.lines.pop(self.pos)
            for k, v in value.items():
                self.lines.insert(self.pos, _Line(indent, f"{k}: {v!r}", line.no))
                self.pos += 1
            self.pos = self.pos - len(value)
            return self._parse_pair(self.lines[self.pos], indent)

        m = _KEY_RE.match(stripped)
        if not m:
            raise MiniYamlError("不是合法的 key: value 行", line.no, line.text)
        key = _parse_key(m.group(1).strip(), line.no, stripped)
        rest = (m.group(2) if m.group(2) is not None else (m.group(3) or "")).strip()
        self.pos += 1
        if rest:
            return key, _parse_scalar(rest, line.no)
        nxt = self._peek()
        if nxt is not None and nxt.indent > indent:
            return key, self._parse_block(nxt.indent)
        if nxt is not None and nxt.indent == indent and self._is_seq_item(nxt):
            # 允许序列与键同缩进（YAML 常见写法）
            return key, self._parse_seq(indent)
        return key, None


# --------------------------------------------------------------------------- #
# 公共 API
# --------------------------------------------------------------------------- #
def loads(text: str) -> Any:
    """解析受限 YAML 文本。

    :raises MiniYamlError: 遇到不支持语法时（继承 ``ConfigError``，含行号）。
    """
    if not isinstance(text, str):
        raise ConfigError("mini_yaml.loads 需要 str 输入")
    return _Parser(_prepare(text)).parse()


def load(path: str | Any) -> Any:
    """读取并解析文件。文件不存在时抛 ``ConfigError``。"""
    from pathlib import Path as _Path

    p = _Path(path)
    if not p.is_file():
        raise ConfigError(f"配置文件不存在：{p}")
    try:
        raw = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 UTF-8：{p}（{exc.reason}）") from None
    try:
        return loads(raw)
    except MiniYamlError as exc:
        exc.context.setdefault("file", str(p))
        raise
