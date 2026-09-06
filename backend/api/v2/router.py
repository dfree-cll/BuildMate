"""API v2 router assembly."""

from fastapi import APIRouter

from backend.api.v1 import auth
from backend.api.v2 import artifacts, chat, knowledge, modeling, projects, tasks, memory

api_v2_router = APIRouter()
api_v2_router.include_router(auth.router, prefix="/auth", tags=["v2-auth"])
api_v2_router.include_router(projects.router)
api_v2_router.include_router(artifacts.router)
api_v2_router.include_router(knowledge.router)
api_v2_router.include_router(tasks.router)
api_v2_router.include_router(modeling.router)
api_v2_router.include_router(chat.router)
api_v2_router.include_router(memory.router)
