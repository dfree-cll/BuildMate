"""路由聚合（对标 EduAgent 8.6）"""
from fastapi import APIRouter
from backend.api.v1 import auth, unified_chat, agents_api, bim_api

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["认证"])
api_router.include_router(unified_chat.router, prefix="/chat", tags=["统一对话入口"])
api_router.include_router(agents_api.router, tags=["Agent 接口"])
api_router.include_router(bim_api.router, tags=["BIM 审图"])