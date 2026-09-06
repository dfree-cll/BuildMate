"""路由聚合"""
from fastapi import APIRouter
from backend.api.v1 import (auth, unified_chat, bid_review, procurement,
                            negotiation, qa, bim_api,
                            observability)

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["认证"])
api_router.include_router(unified_chat.router, prefix="/chat", tags=["统一对话入口"])
api_router.include_router(bid_review.router, tags=["Agent 接口"])
api_router.include_router(procurement.router, tags=["Agent 接口"])
api_router.include_router(negotiation.router, tags=["Agent 接口"])
api_router.include_router(qa.router, tags=["Agent 接口"])
# Kept only as a hidden migration/regression adapter.  Product traffic uses
# the single v2 ``wall_pipeline`` entry point.
api_router.include_router(bim_api.router, tags=["BIM internal"], include_in_schema=False)
api_router.include_router(observability.router, prefix="/observability", tags=["可观测性"])
