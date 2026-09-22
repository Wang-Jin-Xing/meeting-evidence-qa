"""运行元信息:当前实际生效的模型组合(供界面如实标注来源)。仅认证用户可读。

必须报告"真实生效"的配置:Provider 名不可用时工厂直接报错,
本端点会返回 503 而不是伪装成 mock。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.ai.registry import ProviderConfigError, describe_providers
from app.core.deps import get_current_user
from app.models.org import User

router = APIRouter(tags=["meta"])


@router.get("/meta")
def get_meta(user: User = Depends(get_current_user)):
    from app.core.config import get_settings
    from app.models.enums import CostBasis  # 口径提示用

    try:
        providers = describe_providers()
    except ProviderConfigError as e:
        raise HTTPException(
            status_code=503, detail=f"模型配置不可用(不会回退 mock):{e}"
        )
    s = get_settings()
    return {
        "providers": providers,
        "configured": {
            "asr": s.asr_provider, "llm": s.llm_provider, "embed": s.embed_provider,
        },
        "cost_basis_values": [c.value for c in CostBasis],
        "asr_note": (
            "ASR 为 mock 时,自动转写文本仅用于链路演示;"
            "人工转写导入是权威来源,界面按 transcript source 显示"
        ),
    }
