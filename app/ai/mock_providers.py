"""mock Provider 实现:确定性输出,支持故障注入与延迟,仅用于链路测试。

边界声明(设计约定):
- mock ASR 按分片序号从预置语料顺序取句(语料即虚构会谈对话),
  时间信息一律来自调用方传入的切片边界,ASR 不产生时间;
- mock LLM 为关键词规则,不是语言理解;mock 向量为字符 bigram 哈希,
  仅表达字面重叠,无语义泛化能力。所有指标只证明流程正确;
- 故障注入通过环境变量控制(测试传给 worker 子进程):
  MEET_MOCK_ASR_FAIL_SLICE=N   每次第 N 片转写都失败(持续)
  MEET_MOCK_ASR_FAIL_ON_CALL=N 全局第 N 次调用失败一次
  MEET_MOCK_LLM_FAIL_ON_CALL=N / MEET_MOCK_EMBED_FAIL_ON_CALL=N 同上
  MEET_MOCK_ASR_DELAY_MS=N     每次调用延迟 N 毫秒
  MEET_MOCK_KILL_AFTER_CALL=N  全局第 N 次任意 Provider 调用返回后
                               os._exit(70) 立即自杀(模拟进程死于
                               "模型已响应、结果未提交"窗口,恢复测试用)
  MEET_MOCK_ASR_UNKNOWN_ON_CALL=N 第 N 次 ASR 调用抛结果未知异常
                               (模拟超时/断连:请求已发出,远端结果不可知)
  MEET_MOCK_ASR_HANG_ON_CALL=N 第 N 次 ASR 调用挂起 1 小时
                               (验证调用超时兜底,由 stages 的包装打断)
  MEET_MOCK_REQUEST_LOG=path   Provider 入口调用记录(本地 JSONL):mock Provider
                               方法一被调用就在入口记一行,与 model_run 完全独立。
                               口径必须准确:它证明"我方发出了这次调用及其入参
                               规模",不能证明真实服务商收到请求(真实接入需按
                               服务商请求 ID/账单另行核对)。
  MEET_MOCK_ASR_CORPUS_FILE=path 自定义语料 JSON([["说话人","文本"],...],
                               评测框架用;默认内置虚构语料)
"""
import hashlib
import json
import math
import os
import threading
import time

from app.ai.providers import (
    ASRProvider,
    ASRSlice,
    EmbedProvider,
    LLMProvider,
    MinutesDraft,
    MinutesItemDraft,
    ProviderResultUnknownError,
    QACandidate,
    QAAnswer,
    SegmentForLLM,
)
from app.models.transcript import EMBEDDING_DIM

# 虚构会谈语料(演示与测试共用,语料顺序即对话顺序)
CORPUS = [
    ("张主任(机构)", "今天主要讨论X200监护仪的采购和交付安排"),
    ("李经理(厂商)", "X200的注册证和检测报告我们会在5月20日之前补齐"),
    ("张主任(机构)", "X300型号的保修期是多久?和X200有区别吗"),
    ("李经理(厂商)", "X200整机保修两年,X300保修三年"),
    ("王护士长(机构)", "建议第一批设备5月10日先到货,第二批月底前完成"),
    ("李经理(厂商)", "我们同意分两批交付,第一批5月10日送到"),
    ("张主任(机构)", "安装培训需要几天"),
    ("李经理(厂商)", "培训大概需要三天,由我们负责"),
    ("张主任(机构)", "那培训负责人就定你们这边,具体日期我们再确认"),
    ("李经理(厂商)", "没问题,发票和合同本周内寄出"),
    ("王护士长(机构)", "耗材的价格清单也要一起提供"),
    ("李经理(厂商)", "好的,耗材清单5月15日前发给你们"),
    ("张主任(机构)", "今天的议题都确认了,辛苦各位"),
    ("李经理(厂商)", "感谢,我们尽快推进"),
]


class _CallCounter:
    """进程内全局调用计数(线程安全),供 FAIL_ON_CALL 注入。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def next(self, key: str) -> int:
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            return self._counts[key]


_counter = _CallCounter()


def _fail_on_call(key: str) -> bool:
    """支持逗号分隔列表:MEET_MOCK_X_FAIL_ON_CALL=1,2,3 表示第1/2/3次调用各失败一次。"""
    v = os.environ.get(f"MEET_MOCK_{key}_FAIL_ON_CALL")
    if v is None:
        return False
    n = _counter.next(key)
    return str(n) in [x.strip() for x in v.split(",")]


def _kill_after_call() -> None:
    """全局第 N 次任意 Provider 调用返回后立即自杀(不经任何清理/提交)。

    模拟进程死于"模型已响应、结果未提交"窗口:P2 恢复测试专用。
    计数与 FAIL_ON_CALL 各自独立。
    """
    v = os.environ.get("MEET_MOCK_KILL_AFTER_CALL")
    if v is None:
        return
    n = _counter.next("KILL")
    if str(n) == v.strip():
        os._exit(70)


def _log_request(provider: str, call: str, **detail) -> None:
    """Provider 入口调用记录:mock Provider 方法被调用即在本地追加一行 JSON。

    口径:证明"这次调用确实发出且进入 Provider 实现",与 model_run 独立
    (后者只证明调用意图与远端结果状态)。它**不**证明真实服务商收到请求,
    也不证明计费次数;真实 Provider 接入后需以服务商请求 ID/账单另行核对。
    记录点在各 Provider 方法最开头(先于延迟/故障注入)。
    """
    path = os.environ.get("MEET_MOCK_REQUEST_LOG")
    if not path:
        return
    rec = {"ts": round(time.time(), 3), "provider": provider, "call": call, **detail}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


_corpus_cache = None


def get_corpus() -> list[tuple[str, str]]:
    """当前语料:MEET_MOCK_ASR_CORPUS_FILE 指定的自定义语料,否则内置。

    进程级缓存(worker 子进程启动后语料不变)。评测框架用自定义语料
    构造不同会谈内容,实现按会谈划分的 dev/test 数据。
    """
    global _corpus_cache
    if _corpus_cache is None:
        path = os.environ.get("MEET_MOCK_ASR_CORPUS_FILE")
        if path:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            _corpus_cache = [(str(s), str(t)) for s, t in data]
        else:
            _corpus_cache = CORPUS
    return _corpus_cache


def _unknown_on_call(key: str) -> bool:
    """第 N 次调用抛结果未知(超时/断连语义:请求已发出,结果不可知)。"""
    v = os.environ.get(f"MEET_MOCK_{key}_UNKNOWN_ON_CALL")
    if v is None:
        return False
    n = _counter.next(f"{key}_UNKNOWN")
    return str(n) in [x.strip() for x in v.split(",")]


def _hang_on_call(key: str) -> bool:
    """第 N 次调用挂起 1 小时(由 stages 的调用超时包装打断)。"""
    v = os.environ.get(f"MEET_MOCK_{key}_HANG_ON_CALL")
    if v is None:
        return False
    n = _counter.next(f"{key}_HANG")
    return str(n) in [x.strip() for x in v.split(",")]


class MockASR(ASRProvider):
    name = "mock-asr"
    model_name = "mock-asr-v1"
    is_mock = True  # 本地确定性实现,不产生真实费用

    def transcribe_slice(self, audio: bytes, slice_index: int) -> ASRSlice:
        # 入口即记录(先于延迟/故障注入):证明本次调用确实发出
        _log_request("asr", "transcribe_slice",
                     slice_index=slice_index, audio_bytes=len(audio))
        if _hang_on_call("ASR"):
            time.sleep(3600)  # 由 stages 调用超时包装打断
        if _unknown_on_call("ASR"):
            raise ProviderResultUnknownError(
                "[mock] 注入: ASR 调用结果未知(超时/断连语义)"
            )
        delay = os.environ.get("MEET_MOCK_ASR_DELAY_MS")
        if delay:
            time.sleep(int(delay) / 1000)
        fail_slice = os.environ.get("MEET_MOCK_ASR_FAIL_SLICE")
        if fail_slice is not None and int(fail_slice) == slice_index:
            raise RuntimeError(f"[mock] 注入: 分片 {slice_index} 转写失败")
        if _fail_on_call("ASR"):
            raise RuntimeError("[mock] 注入: ASR 调用失败(第N次)")
        speaker, text = get_corpus()[slice_index % len(get_corpus())]
        _kill_after_call()
        return ASRSlice(speaker=speaker, text=text, is_mock=True)


class MockLLM(LLMProvider):
    """关键词规则生成纪要草稿:确定性,禁止补造,缺字段一律留空+待确认。"""
    name = "mock-llm"
    model_name = "mock-llm-rules-v1"
    prompt_version = "mock-rules-v1"
    is_mock = True

    def generate_minutes(self, segments: list[SegmentForLLM]) -> MinutesDraft:
        _log_request("llm", "generate_minutes", segments=len(segments))
        if _fail_on_call("LLM"):
            raise RuntimeError("[mock] 注入: LLM 调用失败(第N次)")
        items: list[MinutesItemDraft] = []
        if segments:
            items.append(
                MinutesItemDraft(
                    type="topic",
                    text=segments[0].text[:80],
                    certainty="discussed",
                    evidence_segment_ids=[segments[0].segment_id],
                )
            )
        for seg in segments:
            text = seg.text
            # 日期提取(仅来自原文,禁止编造)
            due = _extract_date(text)
            certainty = "discussed"
            if any(k in text for k in ("同意", "确认", "决定", "没问题")):
                certainty = "decided"
            elif any(k in text for k in ("建议", "考虑")):
                certainty = "suggested"
            # 待办:承诺类动词
            if any(k in text for k in ("补齐", "寄出", "提供", "送到", "发给", "负责")):
                items.append(
                    MinutesItemDraft(
                        type="todo",
                        text=text[:120],
                        certainty=certainty,
                        owner=seg.speaker if seg.speaker and "厂商" in seg.speaker else None,
                        due_date=due,
                        evidence_segment_ids=[seg.segment_id],
                        needs_confirmation=(due is None),
                    )
                )
            elif certainty == "decided":
                items.append(
                    MinutesItemDraft(
                        type="decision",
                        text=text[:120],
                        certainty=certainty,
                        owner=None,
                        due_date=due,
                        evidence_segment_ids=[seg.segment_id],
                        needs_confirmation=True,
                    )
                )
        _kill_after_call()
        return MinutesDraft(items=items, is_mock=True)

    def answer_question(self, question: str, candidates: list[QACandidate]) -> QAAnswer:
        """mock 问答:焦点线索启发式 + 抽取式回答。

        能力边界(如实声明):充分性判断是关键词启发式,不是语义理解——
        仅检查问题焦点(负责人/时间点/时长)在候选原文中是否有对应线索,
        无法验证线索与问题对象的对应关系(如把 X200 的日期当成 X300 的答案);
        text=None 表示焦点线索缺失,判定证据不足以回答,不拼接候选冒充答案。
        """
        if _fail_on_call("QA"):
            raise RuntimeError("[mock] 注入: QA 调用失败(第N次)")
        if not self._has_focus_clue(question, candidates):
            return QAAnswer(text=None, cited_segment_ids=[], is_mock=True)
        parts = [f"{c.speaker or '未知发言者'}:{c.text}" for c in candidates]
        text = "根据会谈转写记录:" + ";".join(parts)
        _kill_after_call()
        return QAAnswer(
            text=text,
            cited_segment_ids=[c.segment_id for c in candidates],
            is_mock=True,
        )

    @staticmethod
    def _has_focus_clue(question: str, candidates: list[QACandidate]) -> bool:
        """焦点线索启发式(确定性规则):
        问负责人 → 候选须含"负责";问时间点 → 候选须含日期模式;
        问时长 → 候选须含数字+时长单位(年/个月/天/周)。"""
        import re

        texts = [c.text for c in candidates]
        if ("谁" in question or "负责人" in question) and not any(
            "负责" in t for t in texts
        ):
            return False
        if any(k in question for k in ("什么时候", "几号", "日期")) and not any(
            _extract_date(t) for t in texts
        ):
            return False
        if ("多久" in question or "多长" in question) and not any(
            re.search(r"\d+(年|个月|天|周)", t) for t in texts
        ):
            return False
        return True


def _extract_date(text: str) -> str | None:
    """从原文提取日期,找不到返回 None(禁止补造)。"""
    import re

    m = re.search(r"(\d{1,2}月\d{1,2}[日号])", text)
    if m:
        return m.group(1)
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    return None


class MockEmbed(EmbedProvider):
    """字符 bigram 哈希向量:确定性,字面重叠→高余弦,无语义泛化。"""
    name = "mock-embed"
    model_name = "mock-embed-bigram-v1"
    is_mock = True
    embedding_dim = EMBEDDING_DIM  # 与向量列同维度,工厂接入校验点(D-03)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        _log_request("embed", "embed_batch", batch_size=len(texts))
        if _fail_on_call("EMBED"):
            raise RuntimeError("[mock] 注入: Embed 调用失败(第N次)")
        vectors = [self._embed_one(t) for t in texts]
        _kill_after_call()
        return vectors

    @staticmethod
    def _embed_one(text: str) -> list[float]:
        vec = [0.0] * EMBEDDING_DIM
        normalized = "".join(text.split())
        for i in range(len(normalized) - 1):
            bigram = normalized[i : i + 2]
            idx = int.from_bytes(
                hashlib.sha256(bigram.encode("utf-8")).digest()[:4], "big"
            ) % EMBEDDING_DIM
            vec[idx] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [round(v / norm, 8) for v in vec]


# Provider 的选择(按配置、不可用即报错)在 app/ai/registry.py:
# 本模块只提供 mock 实现,不做"默认值"决策,避免配置错误被静默兜底。