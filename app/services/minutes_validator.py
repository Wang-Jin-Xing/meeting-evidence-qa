"""纪要结构化校验(层1,程序可判定,硬失败)。

只负责形式正确性:
- 引用存在且属于该录音的转写版本;
- decision/todo 必须带证据引用;
- owner/due_date 为空时必须 needs_confirmation(空值策略)。
"引用是否语义支持结论"、"负责人/日期是否有原文依据"属语义复核
(LLM 辅助 + 人工确认),本校验不做、也不冒充能做。
"""
from pydantic import BaseModel, Field, ValidationError
from typing import Literal


class MinutesItem(BaseModel):
    type: Literal["topic", "discussion", "decision", "todo", "needs_confirmation"]
    text: str
    certainty: Literal["suggested", "discussed", "decided"]
    owner: str | None = None
    due_date: str | None = None
    evidence_segment_ids: list[int] = Field(default_factory=list)
    needs_confirmation: bool = False


class MinutesContent(BaseModel):
    items: list[MinutesItem]


class MinutesValidationError(Exception):
    pass


def validate_minutes(content: dict, valid_segment_ids: set[int]) -> MinutesContent:
    """校验并规范化纪要内容;不合法抛 MinutesValidationError。"""
    try:
        parsed = MinutesContent.model_validate(content)
    except ValidationError as e:
        raise MinutesValidationError(f"纪要结构不合法: {e}") from e

    known = set(valid_segment_ids)
    for i, item in enumerate(parsed.items):
        unknown = set(item.evidence_segment_ids) - known
        if unknown:
            raise MinutesValidationError(
                f"条目{i}: 引用不存在的片段 {sorted(unknown)}"
            )
        if item.type in ("decision", "todo") and not item.evidence_segment_ids:
            raise MinutesValidationError(
                f"条目{i}: {item.type} 类型必须关联证据片段"
            )
        # 空值策略仅约束行动类条目(decision/todo):
        # 负责人/截止日期缺失时必须标记待确认(禁止补造);
        # topic/discussion 属描述性内容,不适用
        if item.type in ("decision", "todo") and (
            item.owner is None or item.due_date is None
        ) and not item.needs_confirmation:
            raise MinutesValidationError(
                f"条目{i}: 负责人或截止日期缺失时必须标记待确认(禁止补造)"
            )
    return parsed
