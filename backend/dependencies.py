"""依赖注入：get_current_user（JWT）+ LLM 端点限流

H6：JWT 库从 python-jose 迁移到 PyJWT（jose 3.3 存在已知 CVE-2024-33663/33664，
且项目同时引入两套 JWT 库冗余；token 格式 HS256 不变，旧 token 兼容）
"""
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from backend.config import get_settings

settings = get_settings()
bearer_scheme = HTTPBearer(auto_error=False)

# ── H4：令牌吊销/限流（多实例共享存储，Redis 优先内存回退）──────────────────
from backend.core.state_store import state_store

_JTI_SET = "jwt:revoked_jti"
_VER_PREFIX = "jwt:ver:"
_LLM_RL_PREFIX = "rl:llm:"
# token_version 检查结果缓存 60s——角色变更最迟 1 分钟生效，同时避免每请求查库
_VER_CACHE_TTL = 60


async def _token_version_ok(user_id: str, token_ver) -> bool:
    cached = await state_store.get_kv(f"{_VER_PREFIX}{user_id}")
    if cached is not None:
        return int(token_ver or 0) == int(cached)
    try:
        from sqlalchemy import text
        from backend.db.session import engine
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT token_version FROM users WHERE id = :id"
            ), {"id": user_id})).fetchone()
    except Exception:
        return True   # users 表不可用（离线 demo）→ 放行
    if row is None:
        return True   # mock 用户无 DB 行 → 放行
    ver = int(row[0] or 0)
    await state_store.set_kv(f"{_VER_PREFIX}{user_id}", str(ver), _VER_CACHE_TTL)
    return int(token_ver or 0) == ver


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    exc = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的认证凭证")
    if credentials is None:
        raise exc
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret,
                             algorithms=[settings.jwt_algorithm])
        user_id = payload.get("sub")
        if not user_id:
            raise exc
        # H4：刷新令牌不得当作访问令牌使用
        if payload.get("type", "access") != "access":
            raise exc
        # H4：jti 黑名单（已登出的令牌立即失效；TTL 与刷新令牌寿命对齐）
        if payload.get("jti") and await state_store.in_set(_JTI_SET, payload["jti"]):
            raise exc
    except jwt.InvalidTokenError:
        raise exc
    # H4：token_version 校验（logout/改密/改角色后旧令牌失效）
    if not await _token_version_ok(str(user_id), payload.get("ver", 0)):
        raise exc
    return {
        "user_id": str(user_id),
        "role": payload.get("role", "user"),
        "tenant_id": payload.get("tenant_id", settings.default_tenant_id),
        "jti": payload.get("jti", ""),
    }


def require_role(*roles: str):
    """角色校验依赖：require_role("admin") 或 require_role("admin", "teacher")
    用法：current_user: dict = Depends(require_role("admin"))
    """
    async def _checker(current_user: dict = Depends(get_current_user)) -> dict:
        if current_user["role"] not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权限执行该操作")
        return current_user
    return _checker


# ── LLM 端点限流（M1）：每用户固定窗口计数，防认证用户刷爆 LLM 成本 ──────────
_LLM_LIMIT = 30          # 每用户每分钟最大 LLM 请求数
_LLM_WINDOW = 60         # 秒


async def llm_rate_limit(current_user: dict = Depends(get_current_user)) -> dict:
    """挂在走 LLM 的端点上：超限返回 429（登录端点的暴力破解限流见 auth.py）"""
    from backend.core.state_store import state_store
    n = await state_store.incr_window(f"{_LLM_RL_PREFIX}{current_user['user_id']}", _LLM_WINDOW)
    if n > _LLM_LIMIT:
        raise HTTPException(status_code=429, detail="请求过于频繁，请 1 分钟后再试")
    return current_user


__all__ = ["get_current_user", "require_role", "llm_rate_limit"]
