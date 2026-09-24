"""哈希工具（零依赖）。"""

from __future__ import annotations

import hashlib
import re

__all__ = ["sha256_text", "sha256_bytes", "short_hash", "hostname_hash", "normalize_hex", "HOSTNAME_SALT_ENV"]

#: 主机名哈希用的环境变量名（不落盘真实主机名）。
HOSTNAME_SALT_ENV = "KAOYANBENCH_HOST_SALT"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """对文本做 sha256，**先编码为 UTF-8**（保证跨平台一致）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_hash(text: str, length: int = 12) -> str:
    if length <= 0:
        raise ValueError("length 必须为正整数")
    return sha256_text(text)[:length]


def hostname_hash(salt: str = "") -> str:
    """主机名哈希：只落盘哈希不落盘主机名（隐私 + 可复现对比）。"""
    import os
    import platform

    raw = f"{salt}|{platform.node()}"
    return sha256_text(raw)[:16]


def normalize_hex(value: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", value).lower()
