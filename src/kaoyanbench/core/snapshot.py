"""Snapshot 快照读写与校验（方案 4.11 / 5.9 / B-20）。

目录结构（方案 3.4）：
```
benchmark/snapshots/<task_id>/
├── index.json           # {task_id, captured_at, pages: [...]}
├── meta.json            # 兼容别名（= index.json 内容）
└── pages/<sha256>.html
```

L1 评分层回放（P0）：``--offline-replay`` 下 Grader 只用快照内容与
``fetched_at`` 判定，**不发起任何网络请求**。
L2 Agent 层回放代理为 P2，仅保留 ``KAOYANBENCH_REPLAY_DIR`` 环境变量语义。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..utils.hashing import sha256_bytes
from ..utils.text import html_to_text, safe_str
from ..utils.timex import now_iso
from .errors import SnapshotError
from .models import SnapshotIndex, SnapshotPage, Source

__all__ = [
    "INDEX_FILENAME",
    "META_FILENAME",
    "PAGES_DIRNAME",
    "SnapshotStore",
    "VerifyIssue",
    "fetch_url",
    "sources_from_snapshots",
    "DEFAULT_USER_AGENT",
]

INDEX_FILENAME = "index.json"
META_FILENAME = "meta.json"
PAGES_DIRNAME = "pages"

DEFAULT_USER_AGENT = "KaoyanBench/1.1 (+https://github.com/moyetian/Kaoyan_chain_Bench)"
DEFAULT_TIMEOUT = 20

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass
class VerifyIssue:
    task_id: str
    level: str  # error | warn
    message: str
    page: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "level": self.level,
            "message": self.message,
            "page": self.page,
        }


class SnapshotStore:
    """快照库门面（root = ``benchmark/snapshots``）。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    # -- 路径 --------------------------------------------------------------
    def task_dir(self, task_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(task_id or "unknown"))
        return self.root / safe

    def index_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / INDEX_FILENAME

    # -- 读 ----------------------------------------------------------------
    def has(self, task_id: str) -> bool:
        return self.index_path(task_id).is_file()

    def list_tasks(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            p.parent.name for p in self.root.glob(f"*/{INDEX_FILENAME}") if p.is_file()
        )

    def load_index(self, task_id: str) -> SnapshotIndex:
        path = self.index_path(task_id)
        if not path.is_file():
            raise SnapshotError(f"任务 {task_id} 没有快照索引（snapshots/{task_id}/{INDEX_FILENAME}）")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise SnapshotError(f"快照索引损坏：snapshots/{task_id}/{INDEX_FILENAME}") from None
        if not isinstance(data, Mapping):
            raise SnapshotError(f"快照索引顶层必须是对象：snapshots/{task_id}/{INDEX_FILENAME}")
        index = SnapshotIndex.from_dict(data)
        if not index.task_id:
            index.task_id = task_id
        return index

    # -- 写 ----------------------------------------------------------------
    def save_page(
        self,
        task_id: str,
        url: str,
        html: bytes,
        title: str = "",
        http_status: int | None = 200,
        *,
        fetched_at: str | None = None,
        replace: bool = True,
    ) -> SnapshotPage:
        """保存一个页面，按内容 sha256 命名（同内容天然幂等）。"""
        folder = self.task_dir(task_id)
        pages = folder / PAGES_DIRNAME
        pages.mkdir(parents=True, exist_ok=True)

        raw = html if isinstance(html, bytes) else str(html).encode("utf-8")
        digest = sha256_bytes(raw)
        relative = f"{PAGES_DIRNAME}/{digest}.html"
        target = folder / relative
        if not target.exists() or replace:
            target.write_bytes(raw)

        page = SnapshotPage(
            url=str(url),
            title=title or _extract_title(raw),
            fetched_at=fetched_at or now_iso(None),
            content_sha256=digest,
            http_status=http_status,
            snapshot_path=relative,
            bytes=len(raw),
        )
        self._upsert_page(task_id, page)
        return page

    def _upsert_page(self, task_id: str, page: SnapshotPage) -> None:
        folder = self.task_dir(task_id)
        folder.mkdir(parents=True, exist_ok=True)
        if self.has(task_id):
            index = self.load_index(task_id)
        else:
            index = SnapshotIndex(task_id=task_id, captured_at=now_iso(None), pages=[])
        replaced = False
        for position, existing in enumerate(index.pages):
            if existing.url == page.url:
                index.pages[position] = page
                replaced = True
                break
        if not replaced:
            index.pages.append(page)
        index.pages.sort(key=lambda p: (p.url, p.content_sha256))
        index.captured_at = index.captured_at or page.fetched_at
        payload = json.dumps(index.to_dict(), ensure_ascii=False, indent=2)
        (folder / INDEX_FILENAME).write_text(payload, encoding="utf-8")
        (folder / META_FILENAME).write_text(payload, encoding="utf-8")

    # -- 校验 --------------------------------------------------------------
    def verify(self, task_id: str | None = None) -> list[VerifyIssue]:
        """校验 hash 与文件存在性。``task_id=None`` → 校验全部任务。"""
        targets = [task_id] if task_id else self.list_tasks()
        issues: list[VerifyIssue] = []
        for current in targets:
            try:
                index = self.load_index(current)
            except SnapshotError as exc:
                issues.append(VerifyIssue(current, "error", str(exc.message)))
                continue
            for page in index.pages:
                rel = page.snapshot_path or f"{PAGES_DIRNAME}/{page.content_sha256}.html"
                if ".." in Path(rel).parts:
                    issues.append(VerifyIssue(current, "error", "快照路径非法（含 ..）", rel))
                    continue
                target = self.task_dir(current) / rel
                if not target.is_file():
                    issues.append(
                        VerifyIssue(current, "error", f"快照文件缺失：{rel}", rel)
                    )
                    continue
                actual = sha256_bytes(target.read_bytes())
                if actual != page.content_sha256:
                    issues.append(
                        VerifyIssue(
                            current,
                            "error",
                            f"hash 不匹配：期望 {page.content_sha256[:12]}…，实际 {actual[:12]}…",
                            rel,
                        )
                    )
                if not page.url:
                    issues.append(
                        VerifyIssue(current, "warn", "快照缺少 url，无法用于来源比对", rel)
                    )
                if not page.fetched_at:
                    issues.append(
                        VerifyIssue(current, "warn", "快照缺少 fetched_at，时效性不可判定", rel)
                    )
        return issues

    # -- 查找 --------------------------------------------------------------
    def find_by_url(self, task_id: str, url: str) -> SnapshotPage | None:
        try:
            index = self.load_index(task_id)
        except SnapshotError:
            return None
        for page in index.pages:
            if page.url == url:
                return page
        return None

    def find_by_sha(self, task_id: str, sha: str) -> SnapshotPage | None:
        try:
            index = self.load_index(task_id)
        except SnapshotError:
            return None
        needle = str(sha).lower()
        for page in index.pages:
            if page.content_sha256.lower() == needle:
                return page
        return None

    def read_page_text(self, task_id: str, page: SnapshotPage) -> str:
        """读取快照页面的**纯文本**（供 Grader 在回放模式下使用）。"""
        rel = page.snapshot_path or f"{PAGES_DIRNAME}/{page.content_sha256}.html"
        if ".." in Path(rel).parts:
            return ""
        target = self.task_dir(task_id) / rel
        if not target.is_file():
            return ""
        try:
            raw = target.read_bytes()
        except OSError:
            return ""
        if raw[:4] == b"%PDF":
            from ..utils.text import safe_str as _s

            return _s(raw[:0])  # PDF 快照不做文本抽取（v1.0 范围外）
        return html_to_text(raw.decode("utf-8", errors="replace"))

    def pages(self, task_id: str) -> list[SnapshotPage]:
        try:
            return list(self.load_index(task_id).pages)
        except SnapshotError:
            return []


# --------------------------------------------------------------------------- #
# 最小抓取器（P1，需联网；失败不影响离线路径）
# --------------------------------------------------------------------------- #
def fetch_url(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
) -> tuple[bytes, int, str]:
    """抓取单 URL，返回 ``(内容 bytes, http_status, final_url)``。

    超时 / 4xx / 5xx 一律抛 :class:`SnapshotError`（可读信息，无堆栈）。
    """
    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "text/html,*/*"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
            status = int(getattr(response, "status", 200) or 200)
            final = str(getattr(response, "url", url) or url)
            return body, status, final
    except urllib.error.HTTPError as exc:
        raise SnapshotError(f"抓取失败：HTTP {exc.code}") from None
    except (TimeoutError, urllib.error.URLError):
        raise SnapshotError("抓取失败：网络不可达或超时") from None
    except (OSError, ValueError):
        raise SnapshotError("抓取失败：URL 非法或无法访问") from None


def _extract_title(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - decode 已容错
        return ""
    match = _TITLE_RE.search(text)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()[:200]


# --------------------------------------------------------------------------- #
# L1 回放：把快照库转成 Source 列表
# --------------------------------------------------------------------------- #
def sources_from_snapshots(
    snapshot_dir: str | Path,
    task_id: str | None = None,
) -> list[Source]:
    """把快照索引转成 :class:`Source` 列表（``--offline-replay`` 的评分语料）。

    ``task_id`` 为 ``None`` 时读取 ``snapshot_dir`` 下**全部**任务。
    """
    base = Path(snapshot_dir)
    if task_id:
        candidates = [base / task_id]
    else:
        candidates = [p.parent for p in sorted(base.glob(f"*/{INDEX_FILENAME}"))]
    out: list[Source] = []
    for folder in candidates:
        index_file = folder / INDEX_FILENAME
        if not index_file.is_file():
            continue
        try:
            data = json.loads(index_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        index = SnapshotIndex.from_dict(data)
        for page in index.pages:
            out.append(
                Source(
                    url=page.url or None,
                    title=page.title or None,
                    domain=_domain_of(page.url),
                    fetched_at=page.fetched_at or None,
                    content_sha256=page.content_sha256 or None,
                    snapshot_path=f"{folder.name}/{page.snapshot_path}",
                    evidence_level="E0",
                    evidence_reason="来自本地快照（offline replay）",
                    clicked=True,
                )
            )
    out.sort(key=lambda s: (safe_str(s.url), safe_str(s.content_sha256)))
    return out


def _domain_of(url: str | None) -> str | None:
    if not url:
        return None
    text = url.strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split(":", 1)[0]
    return text or None


def snapshot_pages_to_source(
    store: SnapshotStore, task_id: str, pages: Iterable[SnapshotPage]
) -> list[Source]:
    """把指定快照页转成 Source（保留快照相对路径，供 citation 定位）。"""
    out: list[Source] = []
    for page in pages:
        out.append(
            Source(
                url=page.url or None,
                title=page.title or None,
                domain=_domain_of(page.url),
                fetched_at=page.fetched_at or None,
                content_sha256=page.content_sha256 or None,
                snapshot_path=page.snapshot_path or None,
                evidence_level="E0",
                evidence_reason="本地快照回放",
            )
        )
    return sorted(out, key=lambda s: (safe_str(s.url), safe_str(s.content_sha256)))


def no_network_guard() -> Sequence[str]:
    """返回会被 L1 回放禁用的网络入口名称（供单测断言 socket 未被调用）。"""
    return ("socket.create_connection", "urllib.request.urlopen")
