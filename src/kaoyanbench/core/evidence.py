"""证据等级判定 E0~E5（方案 2.5 / 4.5 / B-12）。

规则**不硬编码**，全部来自 ``config/graders/source_levels.yaml``：

```yaml
default_level: E1
no_source: E0
official_domains: [".edu.cn", ".edu", ".gov.cn", ...]
official_level: E4
current_year_boost: E5     # 官方域名 + 年份匹配当年 → 升到 E5
path_rules:                # 路径关键词 → 等级
  - pattern: "/zsml/"
    level: E5
    reason: "招生目录页"
...
domain_rules:
  - suffix: "chsi.com.cn"
    level: E5
third_party_domains: [...]
year_window: 1             # 来源年份与任务年份差距在此之内算「当年」
```

判定为**纯函数**：同输入（source + rules + 任务年份 + 注入的 now）必得同输出。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..utils.text import normalize, safe_str
from .models import EVIDENCE_LEVELS, Source

__all__ = [
    "EvidenceRules",
    "EvidenceVerdict",
    "DEFAULT_RULES",
    "load_rules",
    "judge_source",
    "judge_sources",
    "is_official_level",
    "level_rank",
    "count_at_least",
    "domain_matches",
]

_LEVEL_ORDER = {level: index for index, level in enumerate(EVIDENCE_LEVELS)}


def level_rank(level: str) -> int:
    """``E0`` → 0 … ``E5`` → 5；未知等级按 0 处理（保守）。"""
    return _LEVEL_ORDER.get(str(level).upper(), 0)


def is_official_level(level: str) -> bool:
    """E4 / E5 视为官方（方案 4.5 ``is_official``）。"""
    return level_rank(level) >= 4


def count_at_least(sources: Sequence[Source], level_min: str) -> int:
    """统计证据等级 >= ``level_min`` 的来源数量。"""
    threshold = level_rank(level_min)
    return sum(1 for s in sources if level_rank(s.evidence_level) >= threshold)


def domain_matches(domain: str | None, suffixes: Iterable[str]) -> bool:
    """域名后缀匹配：``www.northplain.edu.cn`` 命中 ``.edu.cn``。"""
    if not domain:
        return False
    text = str(domain).strip().lower().lstrip(".")
    for raw in suffixes:
        suffix = str(raw).strip().lower().lstrip(".")
        if not suffix:
            continue
        if text == suffix or text.endswith("." + suffix):
            return True
    return False


#: 未提供配置文件时的**保守**默认（与 config/graders/source_levels.yaml 保持一致语义）。
DEFAULT_RULES: dict[str, Any] = {
    "default_level": "E1",
    "no_source_level": "E0",
    "official_domains": [".edu.cn", ".edu", ".gov.cn", ".gov", ".org.cn"],
    "official_level": "E4",
    "current_year_level": "E5",
    "third_party_domains": ["zhihu.com", "baidu.com", "sohu.com", "csdn.net", "bilibili.com"],
    "third_party_level": "E2",
    "high_trust_third_party": ["chsi.com.cn", "kaoyan.com", "eol.cn"],
    "high_trust_level": "E3",
    "domain_rules": [
        {"suffix": "chsi.com.cn", "level": "E5", "reason": "中国研究生招生信息网（官方招生平台）"},
        {"suffix": "moe.gov.cn", "level": "E5", "reason": "教育部官网"},
    ],
    "path_rules": [
        {"pattern": "zsml", "level": "E5", "reason": "招生目录页"},
        {"pattern": "zsjz", "level": "E5", "reason": "招生简章页"},
        {"pattern": "zhaosheng", "level": "E5", "reason": "招生栏目页"},
        {"pattern": "fsx", "level": "E4", "reason": "复试线栏目页"},
        {"pattern": "news", "level": "E3", "reason": "新闻公告页"},
        {"pattern": "blog", "level": "E1", "reason": "个人博客"},
    ],
    "year_window": 1,
}


class EvidenceRules:
    """证据等级规则容器（从 YAML 构造）。"""

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        merged = dict(DEFAULT_RULES)
        for key, value in (data or {}).items():
            merged[key] = value
        self.data = merged
        self.default_level = str(merged.get("default_level", "E1") or "E1").upper()
        self.no_source_level = str(merged.get("no_source_level", "E0") or "E0").upper()
        self.official_domains = [str(x) for x in (merged.get("official_domains") or [])]
        self.official_level = str(merged.get("official_level", "E4") or "E4").upper()
        self.current_year_level = str(merged.get("current_year_level", "E5") or "E5").upper()
        self.third_party_domains = [str(x) for x in (merged.get("third_party_domains") or [])]
        self.third_party_level = str(merged.get("third_party_level", "E2") or "E2").upper()
        self.high_trust = [str(x) for x in (merged.get("high_trust_third_party") or [])]
        self.high_trust_level = str(merged.get("high_trust_level", "E3") or "E3").upper()
        self.domain_rules = [dict(r) for r in (merged.get("domain_rules") or [])]
        self.path_rules = [dict(r) for r in (merged.get("path_rules") or [])]
        self.year_window = int(merged.get("year_window", 1) or 0)

    # -- 判定 --------------------------------------------------------------
    def judge(
        self,
        source: Source | Mapping[str, Any] | None,
        *,
        task_year: int | None = None,
    ) -> "EvidenceVerdict":
        """判定单个来源的证据等级。"""
        if source is None:
            return EvidenceVerdict(self.no_source_level, "无来源（模型记忆）", None, False)
        url = safe_str(getattr(source, "url", None) if not isinstance(source, Mapping) else source.get("url"))
        domain = safe_str(
            getattr(source, "domain", None) if not isinstance(source, Mapping) else source.get("domain")
        ) or _domain_of(url)
        year = (
            getattr(source, "year", None)
            if not isinstance(source, Mapping)
            else source.get("year")
        )
        content_hash = safe_str(
            getattr(source, "content_sha256", None)
            if not isinstance(source, Mapping)
            else source.get("content_sha256")
        )
        snapshot = safe_str(
            getattr(source, "snapshot_path", None)
            if not isinstance(source, Mapping)
            else source.get("snapshot_path")
        )

        if not url and not domain and not content_hash and not snapshot:
            return EvidenceVerdict(self.no_source_level, "无 URL / 无快照，视为模型记忆", year, False)

        haystack = normalize_loose_url(url)
        level = ""
        reason = ""

        # 1) 域名规则表（最高优先，精确后缀）
        for rule in self.domain_rules:
            suffix = str(rule.get("suffix", "") or "")
            if suffix and domain and domain_matches(domain, [suffix]):
                level = str(rule.get("level", self.official_level)).upper()
                reason = str(rule.get("reason", f"域名 {suffix} 命中规则表"))
                break

        # 2) 官方域名
        is_official = False
        if not level and domain_matches(domain, self.official_domains):
            level = self.official_level
            reason = f"官方域名（命中 {_matched_suffix(domain, self.official_domains)}）"
            is_official = True

        # 3) 高可信第三方
        if not level and domain_matches(domain, self.high_trust):
            level = self.high_trust_level
            reason = "高可信第三方平台"
        # 4) 普通第三方
        if not level and domain_matches(domain, self.third_party_domains):
            level = self.third_party_level
            reason = "第三方内容站点"

        # 5) 路径规则
        if not level:
            for rule in self.path_rules:
                pattern = normalize(str(rule.get("pattern", "")))
                if pattern and pattern in haystack:
                    level = str(rule.get("level", self.default_level)).upper()
                    reason = str(rule.get("reason", f"路径命中 {pattern}"))
                    break

        if not level:
            level = self.default_level
            reason = "普通网页（无匹配规则）"

        is_official = is_official or is_official_level(level)

        # 6) 年份提升：官方 + 年份匹配任务年份 → E5
        if is_official and task_year is not None and year is not None:
            try:
                gap = abs(int(year) - int(task_year))
            except (TypeError, ValueError):
                gap = 99
            if gap <= self.year_window and level_rank(level) < level_rank(self.current_year_level):
                level = self.current_year_level
                reason = f"{reason}；年份 {year} 与任务年份 {task_year} 一致 → 升为当年官方文件"

        return EvidenceVerdict(level, reason, year, is_official)

    def judge_all(
        self,
        sources: Sequence[Source | Mapping[str, Any]] | None,
        *,
        task_year: int | None = None,
    ) -> list["EvidenceVerdict"]:
        return [self.judge(s, task_year=task_year) for s in (sources or [])]


def normalize_loose_url(url: str) -> str:
    """URL 归一化用于路径匹配：小写 + 去协议 + 去查询串（保留路径分隔符）。"""
    text = safe_str(url).strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("#", 1)[0].split("?", 1)[0]
    return text


def _domain_of(url: str) -> str | None:
    if not url:
        return None
    text = url.strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split(":", 1)[0]
    text = text.split("@", 1)[-1]
    return text or None


def _matched_suffix(domain: str | None, suffixes: Iterable[str]) -> str:
    for suffix in suffixes:
        if domain_matches(domain, [suffix]):
            return str(suffix)
    return ""


@dataclass
class EvidenceVerdict:
    level: str
    reason: str
    year: int | None = None
    is_official: bool = False

    def __post_init__(self) -> None:
        if self.level not in EVIDENCE_LEVELS:
            self.level = "E0"


def load_rules(data: Mapping[str, Any] | None = None) -> EvidenceRules:
    """从配置字典构造规则集（``data=None`` → 使用默认规则）。"""
    return EvidenceRules(data)


def judge_source(
    source: Any,
    rules: Mapping[str, Any] | EvidenceRules | None = None,
    *,
    task_year: int | None = None,
) -> EvidenceVerdict:
    """便捷函数：判定单个来源。"""
    engine = rules if isinstance(rules, EvidenceRules) else EvidenceRules(rules)
    return engine.judge(source, task_year=task_year)


def judge_sources(
    sources: Sequence[Any],
    rules: Mapping[str, Any] | EvidenceRules | None = None,
    *,
    task_year: int | None = None,
    annotate: bool = True,
) -> list[Source]:
    """判定并**原地标注** ``Source.evidence_level`` / ``evidence_reason`` / ``is_official``。

    ``annotate=False`` 时返回原始对象不做修改（纯查询场景）。
    """
    engine = rules if isinstance(rules, EvidenceRules) else EvidenceRules(rules)
    out: list[Source] = []
    for raw in sources or []:
        if isinstance(raw, Mapping):
            raw = Source.from_dict(raw)
        verdict = engine.judge(raw, task_year=task_year)
        if annotate and isinstance(raw, Source):
            current = raw.evidence_level
            # 已是显式判定过的更高等级 → 不降级（Agent 上报的判定可信但不越权）
            if level_rank(current) < level_rank(verdict.level) or current in ("", "E0"):
                raw.evidence_level = verdict.level
                raw.evidence_reason = verdict.reason
                raw.is_official = verdict.is_official
                if raw.year is None:
                    raw.year = verdict.year
        out.append(raw)
    return out
