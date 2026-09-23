"""DeepSeek 真实 LLM 适配器(OpenAI 兼容 Chat Completions)。

失败分类(D-34)必须准确:
- 网络中断 / 客户端超时 → ProviderResultUnknownError(请求已发出,远端结果不可知);
- 服务商返回非 2xx → 普通异常(明确失败:对方已应答);
两者在 model_run 中分别留为 unknown 与 failed,不混记。

并发约定:适配器把最近一次响应的 usage 暂存在实例上供记账读取,
因此**一个实例只能串行使用**(worker 单进程串行处理阶段;问答路径每次请求
新建实例)。若将来并发复用 Provider,须改成随结果返回的用量对象。
"""
import json
import os

import httpx

from app.ai.providers import (
    LLMProvider,
    MinutesDraft,
    MinutesItemDraft,
    ProviderResultUnknownError,
    ProviderUsage,
    QAAnswer,
    QACandidate,
    SegmentForLLM,
)
from app.core.config import Settings

PROMPT_VERSION = "deepseek-meetings-v1"

_MINUTES_SYSTEM = """你是医疗机构会谈纪要助手,负责把会谈转写片段整理成结构化纪要。
硬性规则:
1. 只能依据给出的片段内容,禁止补造片段中没有的事实、负责人、日期或结论;
2. evidence_segment_ids 只能使用片段列表中方括号内给出的数字 id;
3. 每条 decision/todo 必须给出证据 id;owner(负责人)或 due_date(截止)在原文中
   找不到时填 null,并把 needs_confirmation 设为 true(禁止猜测补全);
4. type 只能取 topic|discussion|decision|todo|needs_confirmation;
   certainty 只能取 suggested|discussed|decided;
5. 文本用简体中文,尽量短(每条不超过 60 字)。
只输出 JSON,形如:
{"items":[{"type":"decision","text":"...","certainty":"decided","owner":"...",
"due_date":"...","evidence_segment_ids":[12,13],"needs_confirmation":false}]}"""

_QA_SYSTEM = """你是医疗会谈证据问答助手。你只能根据给出的候选片段回答。
硬性规则:
1. 先判断候选是否真的能回答所问问题;不能回答时输出 {"text": null, "cited_segment_ids": []};
2. 严格区分设备型号与事项归属:候选里关于 A 型号的日期/价格/负责人,不能作为 B 型号的答案;
3. cited_segment_ids 只能使用候选编号方括号内的数字,且必须是你实际依据的片段;
4. 回答用简体中文,不超过 120 字,不复述规则,不使用 markdown。
只输出 JSON:{"text":"...","cited_segment_ids":[12]}"""


def _fmt_segments(segments: list[SegmentForLLM]) -> str:
    return "\n".join(
        f"[{s.segment_id}] {s.speaker or '未知发言人'}: {s.text}" for s in segments
    )


def _fmt_candidates(candidates: list[QACandidate]) -> str:
    lines = []
    for c in candidates:
        at = ""
        if c.start_ms is not None:
            at = f" 位置 {c.start_ms // 1000}s-{(c.end_ms or 0) // 1000}s"
        lines.append(f"[{c.segment_id}] {c.speaker or '未知'}{at}: {c.text}")
    return "\n".join(lines)


def _parse_json(content: str) -> dict:
    """解析模型输出的 JSON;剥掉可能的 ``` 包裹。解析失败=明确失败(可重试)。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError(f"模型输出不是 JSON: {content[:160]!r}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"模型输出 JSON 解析失败: {e};原文 {content[:160]!r}") from e


_TYPES = {"topic", "discussion", "decision", "todo", "needs_confirmation"}
_CERTAINTY = {"suggested", "discussed", "decided"}


def _normalize_minutes_item(it: dict) -> MinutesItemDraft:
    """把模型输出约束到契约允许的形状,**只向更保守方向修正**:
    - 未知 type/certainty → discussion/discussed(不升级为"已决定");
    - decision|todo 缺 owner 或 due_date → 强制 needs_confirmation(待人工确认),
      绝不代为补造负责人或日期;
    - 证据 id 只保留数字(越界 id 由服务层引用校验拒绝,不在这里静默剔除)。
    """
    item_type = str(it.get("type") or "discussion")
    if item_type not in _TYPES:
        item_type = "discussion"
    certainty = str(it.get("certainty") or "discussed")
    if certainty not in _CERTAINTY:
        certainty = "discussed"
    owner = (str(it.get("owner")).strip() or None) if it.get("owner") else None
    due = (str(it.get("due_date")).strip() or None) if it.get("due_date") else None
    needs = bool(it.get("needs_confirmation") or False)
    if item_type in ("decision", "todo") and (owner is None or due is None):
        needs = True
    return MinutesItemDraft(
        type=item_type,
        text=str(it.get("text") or "").strip(),
        certainty=certainty,
        owner=owner,
        due_date=due,
        evidence_segment_ids=[
            int(x) for x in (it.get("evidence_segment_ids") or [])
            if str(x).lstrip("-").isdigit()
        ],
        needs_confirmation=needs,
    )


class DeepSeekLLM(LLMProvider):
    name = "deepseek"
    prompt_version = PROMPT_VERSION
    is_mock = False

    def __init__(self, settings: Settings):
        key = settings.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError(
                "DeepSeek 密钥缺失(环境变量 DEEPSEEK_API_KEY)。"
                "不会回退 mock:真实模式必须显式失败。"
            )
        self._key = key
        self._base = settings.deepseek_base_url.rstrip("/")
        self.model_name = settings.deepseek_model
        self._timeout = settings.deepseek_timeout_seconds
        self._max_tokens = settings.deepseek_max_tokens
        self._price_in = settings.llm_price_per_1k_in
        self._price_out = settings.llm_price_per_1k_out
        self.runtime_device = "remote:deepseek"
        self._last_usage: dict = {}

    # ---- 成本口径 ----

    def estimate_cost(self, call: str, units: dict) -> float | None:
        """发出前的费用上界(按最大输出预留)。无单价表 → None(成本未知)。"""
        if self._price_in is None or self._price_out is None:
            return None
        in_tokens = int(units.get("chars", 0) / 1.4) + 400  # 中文约 1.4 字/token
        out_tokens = self._max_tokens
        return round(in_tokens / 1000 * self._price_in
                     + out_tokens / 1000 * self._price_out, 6)

    def usage_for(self, call: str, units: dict, result) -> ProviderUsage | None:
        """tokens 为服务商返回口径;金额按单价表算(未配置则成本未知)。"""
        u = self._last_usage or {}
        tin, tout = u.get("prompt_tokens"), u.get("completion_tokens")
        if self._price_in is None or self._price_out is None:
            return ProviderUsage(basis="unknown", cost_cents=None,
                                 tokens_in=tin, tokens_out=tout)
        cents = round(
            (tin or 0) / 1000 * self._price_in
            + (tout or 0) / 1000 * self._price_out, 6,
        )
        return ProviderUsage(basis="estimated", cost_cents=cents,
                             tokens_in=tin, tokens_out=tout)

    # ---- 调用 ----

    def _chat(self, system: str, user: str) -> str:
        payload = {
            "model": self.model_name,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": self._max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            resp = httpx.post(
                f"{self._base}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=self._timeout,
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            # 请求已发出但没拿到应答:结果未知(不记 failed,可能已计费)
            raise ProviderResultUnknownError(
                f"DeepSeek 调用未完成,结果未知: {type(e).__name__}: {e}"
            ) from e
        if resp.status_code != 200:
            # 服务商已应答 → 明确失败
            raise RuntimeError(
                f"DeepSeek 返回 HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        self._last_usage = data.get("usage") or {}
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"DeepSeek 响应无 choices: {str(data)[:200]}")
        return (choices[0].get("message") or {}).get("content") or ""

    def generate_minutes(self, segments: list[SegmentForLLM]) -> MinutesDraft:
        user = (
            "会谈转写片段(编号即证据 id):\n"
            + _fmt_segments(segments)
            + "\n\n请输出结构化纪要 JSON。"
        )
        content = self._chat(_MINUTES_SYSTEM, user)
        raw = _parse_json(content).get("items") or []
        items = [_normalize_minutes_item(it) for it in raw if isinstance(it, dict)]
        return MinutesDraft(items=items, is_mock=False)

    def answer_question(self, question: str,
                        candidates: list[QACandidate]) -> QAAnswer:
        user = (
            "问题:" + question + "\n\n候选片段(只能引用其编号):\n"
            + _fmt_candidates(candidates)
            + "\n\n若候选不足以回答该问题(含所问型号/事项与候选不符),"
            '请输出 {"text": null, "cited_segment_ids": []}。'
        )
        content = self._chat(_QA_SYSTEM, user)
        data = _parse_json(content)
        text = data.get("text")
        cited = [
            int(x) for x in (data.get("cited_segment_ids") or [])
            if str(x).lstrip("-").isdigit()
        ]
        return QAAnswer(
            text=(str(text).strip() or None) if text else None,
            cited_segment_ids=cited,
            is_mock=False,
        )
