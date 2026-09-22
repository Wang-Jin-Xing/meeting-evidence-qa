"""会谈内检索问答:POST /meetings/{id}/ask。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.org import User
from app.schemas.qa import AnswerOut, QuestionIn
from app.services import qa_service
from app.services import budget
from app.services.access import get_accessible_meeting

router = APIRouter(tags=["qa"])


@router.post("/meetings/{meeting_id}/ask", response_model=AnswerOut)
def ask_meeting(
    meeting_id: int,
    body: QuestionIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """基于该会谈已发布索引的证据问答。

    权限:会谈访问检查(404 不区分无权/不存在)+ 候选 SQL 双注入;
    引用内嵌片段原文与时间位置;无证据时明确返回证据不足。
    """
    meeting = get_accessible_meeting(db, user, meeting_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="会谈不存在或无权访问"
        )
    try:
        return qa_service.ask_question(db, user, meeting, body.question)
    except qa_service.QACitationViolationError as e:
        # 引用越权属服务端协议违规,不返回可能泄露内容的回答
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"问答引用校验失败: {e}",
        )
    except budget.BudgetExceededError as e:
        # 预算耗尽:在发出模型调用之前就拒绝(不是"先调用再记账")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"模型费用预算已耗尽,暂不可回答:{e}",
        )
    except budget.BudgetUnaccountedError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"模型费用不可核算,暂不可回答:{e}",
        )
