"""可观测性 REST 接口（Observability，中间件中台对外出口）

LLM 调用统计（对标 Langfuse 轻量替代）。原挂载于 api/v1/qa.py，
现独立为一级路由，URL 保持不变：GET /api/v1/observability/stats
"""
from fastapi import APIRouter, Depends

from backend.dependencies import get_current_user

router = APIRouter()


@router.get("/stats")
async def obs_stats(hours: int = 24, current_user: dict = Depends(get_current_user)):
    """LLM 调用统计（需登录；内部指标不暴露未认证）"""
    from backend.core.observability import get_call_stats
    return await get_call_stats(hours)
