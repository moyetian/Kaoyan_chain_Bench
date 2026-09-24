"""SemanticGrader + SemanticClient（方案 5.3 / B-14 / B-15）。

★ 两条硬规则：
1. ``build_semantic_client`` 在**无 key 时返回 ``None``**（不抛异常）。
2. 任何语义评分的失败都必须把 ``grader_mode`` 置为 ``degraded`` 并写明原因，
   **不允许静默假装评过**。

降级后的规则化评分公式（方案 5.3 写死）：
    score = 0.5*(要点关键词命中率) + 0.3*(required_fields 覆盖率) + 0.2*(长度/结构合理性)
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ...utils.text import (
    contains_any,
    coverage_ratio,
    normalize,
    safe_str,
    truncate,
)
from ..grader import BaseGrader, GradeContext, run_checks
from ..models import (
    AgentOutput,
    CheckResult,
    RubricCriterion,
    Task,
)

__all__ = [
    "JudgeReply",
    "SemanticClient",
    "build_semantic_client",
    "SemanticGrader",
    "fallback_semantic_score",
]

#: 固定提示词（``prompt_version`` 变化时必须同步升版，方案 R2）。
PROMPT_TEMPLATE = """你是一个严格的评测员。请只根据给定材料判断「待评答案」是否满足「评分标准」。

评分标准：{criterion}

待评答案：
<answer>
{answer}
</answer>

参考材料：
<context>
{context}
</context>

只输出一个 JSON 对象，不要输出任何其它文字，格式：
{{"supported": true|false, "score": 0.0~1.0, "reason": "简短中文理由（<=80字）"}}
"""


@dataclass
class JudgeReply:
    supported: bool | None = None
    score: float | None = None
    reason: str = ""
    raw: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "score": self.score,
            "reason": self.reason,
            "error": self.error,
        }


class SemanticClient:
    """OpenAI 兼容端点的最小客户端（``urllib`` 实现，零第三方依赖）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 2000,
        timeout: int = 60,
        prompt_version: str = "v1",
        api_style: str = "chat_completions",
        max_retries: int = 1,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.timeout = int(timeout)
        self.prompt_version = prompt_version
        self.api_style = api_style
        #: ``invalid_response``（响应非 JSON / 缺字段）的最大重试次数（方案 5.3）
        self.max_retries = max(0, int(max_retries))

    # -- 可用性 ------------------------------------------------------------
    def available(self) -> bool:
        return bool(self.base_url and self.api_key)

    # -- 调用 --------------------------------------------------------------
    def judge(self, criterion: str, answer: str, context: str = "") -> JudgeReply:
        """判定单条标准。任何网络/解析失败都返回带 ``error`` 的 ``JudgeReply``（不抛异常）。

        仅对 ``invalid_response``（响应非 JSON / 缺字段）重试，最多 ``max_retries`` 次
        （方案 5.3）。网络类错误不重试，避免在无网/断网环境下拖长评测时间。
        """
        if not self.available():
            return JudgeReply(error="no_api_key")
        prompt = PROMPT_TEMPLATE.format(
            criterion=criterion,
            answer=truncate(answer, 6000)[0],
            context=truncate(context, 6000)[0],
        )
        reply = self._judge_once(prompt)
        attempts = 0
        while reply.error == "invalid_response" and attempts < self.max_retries:
            attempts += 1
            reply = self._judge_once(prompt)
        return reply

    def _judge_once(self, prompt: str) -> JudgeReply:
        """单次请求 + 解析；失败分类见方案 5.3 降级规则表。"""
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        url = self.base_url
        if not url.endswith("/chat/completions"):
            url = url + "/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return JudgeReply(error=f"http_{exc.code}")
        except (TimeoutError, socket.timeout):
            return JudgeReply(error="network_unreachable")
        except urllib.error.URLError:
            return JudgeReply(error="network_unreachable")
        except OSError:
            return JudgeReply(error="network_unreachable")

        return parse_judge_body(body, raw=body)

    def judge_many(
        self, criteria: Sequence[RubricCriterion], answer: str, context: str = ""
    ) -> list[JudgeReply]:
        return [self.judge(c.question, answer, context) for c in criteria]


def parse_judge_body(body: str, *, raw: str = "") -> JudgeReply:
    """解析评测模型返回体；非 JSON / 缺字段 → ``invalid_response``。"""
    try:
        envelope = json.loads(body)
    except json.JSONDecodeError:
        return JudgeReply(error="invalid_response", raw=truncate(raw or body, 500)[0])

    content = ""
    if isinstance(envelope, dict):
        choices = envelope.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
            if isinstance(message, Mapping):
                content = safe_str(message.get("content")) or safe_str(message.get("reasoning_content"))
        if not content and isinstance(envelope.get("content"), str):
            content = envelope["content"]
    if not content:
        return JudgeReply(error="invalid_response", raw=truncate(raw or body, 500)[0])

    parsed = _extract_json_object(content)
    if parsed is None:
        return JudgeReply(error="invalid_response", raw=truncate(content, 500)[0])

    supported_raw = parsed.get("supported")
    supported: bool | None
    if isinstance(supported_raw, bool):
        supported = supported_raw
    elif isinstance(supported_raw, str):
        lowered = supported_raw.strip().lower()
        supported = True if lowered in ("true", "yes", "1") else (
            False if lowered in ("false", "no", "0") else None
        )
    else:
        supported = None

    score_raw = parsed.get("score")
    score: float | None = None
    if isinstance(score_raw, (int, float)) and not isinstance(score_raw, bool):
        score = max(0.0, min(1.0, float(score_raw)))
    elif isinstance(score_raw, str):
        try:
            score = max(0.0, min(1.0, float(score_raw)))
        except ValueError:
            score = None

    reason = safe_str(parsed.get("reason"))[:200]
    if supported is None and score is None:
        return JudgeReply(error="invalid_response", raw=truncate(content, 500)[0], reason=reason)
    return JudgeReply(supported=supported, score=score, reason=reason, raw=truncate(content, 500)[0])


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        # 去掉 markdown 代码围栏
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    first = stripped.find("{")
    last = stripped.rfind("}")
    if 0 <= first < last:
        try:
            data = json.loads(stripped[first : last + 1])
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None
    return None


def build_semantic_client(
    cfg: Any,
    prompt_config: Mapping[str, Any] | None = None,
) -> SemanticClient | None:
    """按配置构造客户端；**无 api_key → 返回 ``None``**（触发降级，方案 5.3）。

    ``cfg`` 通常传 ``config.grader_model``（``ModelConfig``：有 ``api_key()`` /
    ``base_url`` / ``name`` / ``temperature`` / ``timeout``）。
    ``prompt_config`` 为可选的 ``config/graders/semantic_v1.yaml`` 内容，
    仅用于读取 ``prompt_version``（缺省 ``"v1"``）。
    """
    if cfg is None:
        return None
    api_key = None
    getter = getattr(cfg, "api_key", None)
    if callable(getter):
        api_key = getter()
    if not api_key:
        return None
    base_url = getattr(cfg, "base_url", "") or ""
    if not base_url:
        return None
    prompt_version = str(
        (prompt_config or {}).get("prompt_version")
        or getattr(cfg, "prompt_version", "v1")
        or "v1"
    )
    return SemanticClient(
        base_url=base_url,
        api_key=api_key,
        model=getattr(cfg, "name", "") or "",
        temperature=float(getattr(cfg, "temperature", 0.0) or 0.0),
        max_tokens=int(getattr(cfg, "max_tokens", 2000) or 2000),
        timeout=int(getattr(cfg, "timeout", 60) or 60),
        prompt_version=prompt_version,
        max_retries=int(getattr(cfg, "max_retries", 1) or 0),
    )


# --------------------------------------------------------------------------- #
# 降级规则化评分
# --------------------------------------------------------------------------- #
#: 降级评分上限（对标 DeepEval/RAGAS 实践：无 judge 时的规则化分数不可与
#: 完整语义判定等价，防止 degraded 冒充 full 通过门禁；0.7 保证降级至多踩线，
#: 且回归对比时必须结合 grader_mode 解读）。
DEGRADED_SCORE_CAP = 0.7


def fallback_semantic_score(
    task: Task, output: AgentOutput, criterion: RubricCriterion | None = None
) -> tuple[float, str]:
    """规则化降级评分（方案 5.3 公式）。返回 ``(score 0~1, detail)``。"""
    answer = output.final_answer or ""
    any_of: list[str] = []
    if criterion is not None:
        # criteria 里若写了 any_of（非标准字段，但允许出题人加），优先使用
        extra = getattr(criterion, "any_of", None)
        if isinstance(extra, list):
            any_of = [safe_str(x) for x in extra if safe_str(x)]
    if not any_of:
        for point in task.expected.must_find:
            any_of.extend(point.any_of or point.all_of or [])
    keyword_ratio = (
        sum(1 for kw in any_of if contains_any(answer, [kw])) / len(any_of) if any_of else 0.0
    )
    field_ratio = coverage_ratio(answer, task.expected.required_fields)
    length = len(normalize(answer))
    if length >= 120:
        structure = 1.0
    elif length >= 40:
        structure = 0.5
    else:
        structure = 0.0
    score = 0.5 * keyword_ratio + 0.3 * field_ratio + 0.2 * structure
    capped = min(score, DEGRADED_SCORE_CAP)
    detail = (
        f"降级规则化评分：要点命中率 {keyword_ratio:.2f} × 0.5 + "
        f"必需字段覆盖率 {field_ratio:.2f} × 0.3 + 结构合理性 {structure:.1f} × 0.2"
        f" = {score:.2f}，上限 {DEGRADED_SCORE_CAP:.1f} → {capped:.2f}"
        f"（degraded 不可等同 full，需结合 grader_mode 解读）"
    )
    return max(0.0, min(1.0, capped)), detail


class SemanticGrader(BaseGrader):
    name = "semantic"
    version = "1.0"

    def grader_versions(self, ctx: GradeContext) -> dict[str, Any]:
        client = ctx.semantic_client
        return {
            "deterministic": self.version,
            "semantic_prompt": getattr(client, "prompt_version", "v1"),
            "judge_model": getattr(client, "model", None),
        }

    def evaluate(
        self, task: Task, output: AgentOutput, ctx: GradeContext
    ) -> tuple[list[CheckResult], str, str | None]:
        # 先跑确定性 checks（作为语义分的补充与 hallucination 兜底）
        checks = run_checks(task.grader.checks, task, output, ctx)
        criteria = list(task.grader.rubric.criteria) if task.grader.rubric else []
        if not criteria:
            return checks, "degraded", "not_configured"

        client = ctx.semantic_client
        if client is None or not client.available():
            return checks + _degraded_checks(task, output, criteria), "degraded", "no_api_key"

        replies_failed = False
        failure_reason: str | None = None
        semantic_checks: list[CheckResult] = []
        for criterion in criteria:
            reply = client.judge(criterion.question, output.final_answer, _context_text(task, ctx))
            if reply.error:
                replies_failed = True
                failure_reason = failure_reason or reply.error
                score, detail = fallback_semantic_score(task, output, criterion)
                semantic_checks.append(
                    CheckResult(
                        id=criterion.id,
                        type="semantic_rubric",
                        dimension=criterion.dimension,
                        passed=score >= 0.5,
                        weight=float(criterion.weight),
                        critical=False,
                        detail=f"降级（{reply.error}）：{detail}",
                        judge="semantic",
                    )
                )
                continue
            passed = reply.supported if reply.supported is not None else (
                (reply.score or 0.0) >= 0.5
            )
            reason = reply.reason or ("满足标准" if passed else "不满足标准")
            semantic_checks.append(
                CheckResult(
                    id=criterion.id,
                    type="semantic_rubric",
                    dimension=criterion.dimension,
                    passed=passed,
                    weight=float(criterion.weight),
                    critical=False,
                    detail=f"语义判定：{reason}",
                    judge="semantic",
                )
            )

        if replies_failed:
            mode = "degraded"
            reason = _normalize_reason(failure_reason)
        else:
            mode = "full"
            reason = None
        return checks + semantic_checks, mode, reason


def _normalize_reason(reason: str | None) -> str:
    if reason in ("no_api_key", "network_unreachable", "invalid_response", "semantic_disabled", "not_configured"):
        return str(reason)
    if reason and reason.startswith("http_"):
        return "invalid_response"
    return "invalid_response"


def _degraded_checks(
    task: Task, output: AgentOutput, criteria: Sequence[RubricCriterion]
) -> list[CheckResult]:
    out: list[CheckResult] = []
    for criterion in criteria:
        score, detail = fallback_semantic_score(task, output, criterion)
        out.append(
            CheckResult(
                id=criterion.id,
                type="semantic_rubric",
                dimension=criterion.dimension,
                passed=score >= 0.5,
                weight=float(criterion.weight),
                critical=False,
                detail=f"{detail}",
                judge="semantic",
            )
        )
    return out


def _context_text(task: Task, ctx: GradeContext) -> str:
    """给评测模型的参考材料：优先 fixture 内文本（截断）。"""
    refs = ctx.task_dir / task.references_dir
    if not refs.is_dir():
        return ""
    chunks: list[str] = []
    total = 0
    for path in sorted(refs.rglob("*")):
        if not path.is_file() or total > 12000:
            continue
        if path.suffix.lower() in (".html", ".htm", ".pdf", ".xlsx", ".xls"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.append(text[:4000])
        total += len(chunks[-1])
    return "\n---\n".join(chunks)[:12000]
