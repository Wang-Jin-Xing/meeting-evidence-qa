"""枚举定义。

说明:数据库层统一使用非原生枚举(VARCHAR + CHECK,即 native_enum=False),
避免 PG 原生枚举类型在后续迁移中增删值的维护成本。
"""
from enum import Enum


class UserRole(str, Enum):
    org_admin = "org_admin"
    staff = "staff"


class MeetingVisibility(str, Enum):
    org = "org"              # 本机构全员可见
    restricted = "restricted"  # 创建者 + 白名单 + org_admin


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class StageName(str, Enum):
    prepare = "prepare"
    transcribe = "transcribe"
    minutes = "minutes"
    index = "index"


# 流水线阶段顺序(worker 按此推进)
STAGE_ORDER = [StageName.prepare, StageName.transcribe, StageName.minutes, StageName.index]


class StageStatus(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class SliceStatus(str, Enum):
    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"


class TranscriptSourceType(str, Enum):
    asr = "asr"                        # 任务自动转写
    human_transcription = "human_transcription"  # 人工转写导入(无自动转写版本时)
    human_correction = "human_correction"  # 人工纠错(在被纠错版本之上追加)
    re_asr = "re_asr"                  # 重新转写


class TimestampPrecision(str, Enum):
    # 时间信息来源:音频切片边界(片段级),非模型猜测
    segment_level_from_slice = "segment_level_from_slice"


class MinutesSourceType(str, Enum):
    ai_draft = "ai_draft"
    human_edit = "human_edit"


class MinutesStatus(str, Enum):
    pending_review = "pending_review"
    confirmed = "confirmed"


class ModelRunStatus(str, Enum):
    succeeded = "succeeded"
    failed = "failed"
    unknown = "unknown"  # 超时/断连,结果未知


class CostBasis(str, Enum):
    """费用口径三态。NULL 成本一律是 unknown,不得当作 0 参与预算核算。"""

    estimated = "estimated"                      # 按单价表估算(实际可小幅偏差)
    provider_reported = "provider_reported"      # 服务商返回的用量/账单口径
    unknown = "unknown"                          # 无成本信息,cost_estimate 必须为 NULL


class MappingChangeType(str, Enum):
    unchanged = "unchanged"
    text_edited = "text_edited"
