"""B-12：证据等级 E0~E5 判定；B-20：快照读写 / 校验 / 离线回放。"""

from __future__ import annotations

from pathlib import Path

import pytest

from kaoyanbench.core.evidence import (
    EvidenceRules,
    count_at_least,
    is_official_level,
    judge_source,
    judge_sources,
    level_rank,
)
from kaoyanbench.core.models import Source
from kaoyanbench.core.snapshot import SnapshotStore, sources_from_snapshots

REPO_ROOT = Path(__file__).resolve().parents[1]


def _rules() -> EvidenceRules:
    from kaoyanbench.core.config import load_source_levels

    return EvidenceRules(load_source_levels(path=REPO_ROOT / "config" / "graders" / "source_levels.yaml"))


def test_level_rank() -> None:
    assert level_rank("E0") == 0
    assert level_rank("E5") == 5
    assert is_official_level("E4") is True
    assert is_official_level("E3") is False


def test_no_source_is_e0() -> None:
    verdict = judge_source(None, _rules())
    assert verdict.level == "E0"


def test_official_domain_with_year_is_e5() -> None:
    src = Source(url="https://kaoyan.example.edu.cn/zsml/2025.html", domain="kaoyan.example.edu.cn", title="2025 招生目录")
    verdict = judge_source(src, _rules(), task_year=2025)
    assert level_rank(verdict.level) >= 4


def test_generic_third_party_is_low() -> None:
    src = Source(url="https://weibo.com/x", domain="weibo.com", title="t")
    verdict = judge_source(src, _rules())
    assert level_rank(verdict.level) <= 3


def test_rules_configurable_not_hardcoded() -> None:
    custom = EvidenceRules({"official_domains": [".example.test"], "official_level": "E5", "current_year_level": "E5"})
    src = Source(url="https://a.example.test/x", domain="a.example.test", title="t")
    verdict = judge_source(src, custom)
    assert verdict.level == "E5"


def test_judge_sources_annotates() -> None:
    srcs = [Source(url="https://a.edu.cn/x", domain="a.edu.cn", title="t")]
    out = judge_sources(srcs, _rules(), annotate=True)
    assert out[0].evidence_level != "E0"


def test_count_at_least() -> None:
    srcs = [
        Source(url="u", domain="a.edu.cn", title="t", evidence_level="E5"),
        Source(url="u", domain="b.com", title="t", evidence_level="E2"),
    ]
    assert count_at_least(srcs, "E4") == 1


# --------------------------------------------------------------------------- #
# B-20 snapshot
# --------------------------------------------------------------------------- #
def test_save_and_load_snapshot(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    page = store.save_page("SEARCH-001", "https://a.example.edu.cn/x", b"<html>hi</html>", title="t")
    assert store.has("SEARCH-001")
    idx = store.load_index("SEARCH-001")
    assert len(idx.pages) == 1
    assert idx.pages[0].content_sha256 == page.content_sha256


def test_verify_detects_tampering(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    store.save_page("SEARCH-001", "https://a.example.edu.cn/x", b"<html>hi</html>", title="t")
    # 篡改快照正文
    page = store.pages("SEARCH-001")[0]
    target = tmp_path / page.snapshot_path if not Path(page.snapshot_path).is_absolute() else Path(page.snapshot_path)
    if not target.exists():
        target = tmp_path / "SEARCH-001" / Path(page.snapshot_path).name
        if not target.exists():
            # 兜底：遍历找到该页文件
            target = next(tmp_path.rglob(Path(page.snapshot_path).name))
    target.write_bytes(b"<html>tampered</html>")
    issues = store.verify("SEARCH-001")
    assert issues, "篡改后应报告 hash 不匹配"


def test_verify_clean(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    store.save_page("SEARCH-001", "https://a.example.edu.cn/x", b"<html>hi</html>", title="t")
    assert store.verify("SEARCH-001") == []


def test_sources_from_snapshots(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    store.save_page("SEARCH-001", "https://a.example.edu.cn/zsml/2025.html", b"<html>hi</html>", title="2025 招生目录")
    srcs = sources_from_snapshots(tmp_path, "SEARCH-001")
    assert len(srcs) == 1
    assert srcs[0].url.endswith("2025.html")


def test_no_network_guard_returns_hosts() -> None:
    from kaoyanbench.core.snapshot import no_network_guard

    hosts = no_network_guard()
    assert isinstance(hosts, (list, tuple))


def test_sources_from_snapshots_scoped_by_task_id(tmp_path: Path) -> None:
    """传 ``task_id`` 时只返回该任务的来源（防跨任务污染）。"""
    store = SnapshotStore(tmp_path)
    store.save_page("SEARCH-001", "https://a.example.edu.cn/1.html", b"<html>a</html>", title="A")
    store.save_page("SEARCH-002", "https://b.example.edu.cn/2.html", b"<html>b</html>", title="B")

    scoped = sources_from_snapshots(tmp_path, "SEARCH-001")
    assert [s.url for s in scoped] == ["https://a.example.edu.cn/1.html"]

    # 不传 task_id 时读全部（既有语义，供调用方显式选择）
    assert len(sources_from_snapshots(tmp_path)) == 2


def test_sources_of_offline_replay_scoped_to_task(tmp_path: Path) -> None:
    """``offline_replay`` 下 source 类检查只能用**本任务**快照；无 task_id 时不回放。

    回归点：``checks._sources_of`` 曾漏传 ``task_id``，导致 source_precision /
    source_level / citation_coverage 把快照目录下所有任务的来源都算进来。
    """
    from kaoyanbench.core.checks import _sources_of
    from kaoyanbench.core.grader import GradeContext
    from kaoyanbench.core.models import AgentOutput

    store = SnapshotStore(tmp_path)
    store.save_page("SEARCH-001", "https://a.example.edu.cn/1.html", b"<html>a</html>", title="A")
    store.save_page("SEARCH-002", "https://b.example.edu.cn/2.html", b"<html>b</html>", title="B")

    output = AgentOutput(run_id="r1", agent="mock", task_id="SEARCH-001")
    ctx = GradeContext(
        task_dir=tmp_path / "SEARCH-001",
        workspace_dir=tmp_path,
        task_id="SEARCH-001",
        snapshot_dir=tmp_path,
        offline_replay=True,
    )
    assert [s.url for s in _sources_of(output, ctx)] == ["https://a.example.edu.cn/1.html"]

    class _CtxWithoutTaskId:
        offline_replay = True
        snapshot_dir = tmp_path
        task_id = ""

    # 无法确定任务身份 → 不回放（宁可用 Agent 自报来源，也不跨任务污染）
    assert _sources_of(output, _CtxWithoutTaskId()) == []


def test_repo_snapshot_library_consumable() -> None:
    """仓库 ``benchmark/snapshots`` 必须能被 SnapshotStore 直接消费。

    回归点：``gen_fixtures`` 曾把快照写到 ``benchmark/fixtures/snapshots/``，
    与运行时快照库（``config: evaluation.snapshot_dir``）错位，且 index.json
    的 ``pages`` 是字符串列表而非 SnapshotPage 对象，导致 ``snapshot verify`` 崩溃。
    """
    root = REPO_ROOT / "benchmark" / "snapshots"
    if not root.is_dir():
        pytest.skip("仓库未生成快照库")
    store = SnapshotStore(root)
    task_ids = store.list_tasks()
    assert task_ids, "快照库为空（应至少含 SEARCH-011/012、RES-002）"
    assert store.verify() == [], "快照库校验未通过"
    for task_id in task_ids:
        assert sources_from_snapshots(root, task_id), f"{task_id} 未产出任何 Source"
