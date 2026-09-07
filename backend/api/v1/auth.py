"""登录认证（H4 完整版）：
- 用户源：DB users 表（bcrypt）优先，无 DB 行时回退 mock（离线 demo）
- 双 token：访问令牌（短效）+ 刷新令牌（长效，POST /auth/refresh 换新）
- 吊销：POST /auth/logout —— jti 黑名单（当前令牌）+ DB 用户 bump token_version（全端下线）
- 旧占位/明文哈希登录时透明升级为 bcrypt（存量数据免迁移）
"""
import hmac
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text

from backend.config import get_settings
from backend.core.logger import get_logger
from backend.core.security import hash_password, verify_password
from backend.core.state_store import state_store
from data.mock.mock_users import mock_verify
from backend.db.session import engine
from backend.dependencies import get_current_user, _JTI_SET, _VER_PREFIX

router = APIRouter()
logger = get_logger(__name__)
settings = get_settings()

# ── 登录限流（固定窗口：同用户名+IP 5 次/分钟，防暴力破解；多实例经共享存储）──
_LOGIN_LIMIT = 5          # 每分钟最大尝试
_LOGIN_WINDOW = 60        # 秒
_LOGIN_RL_PREFIX = "rl:login:"


async def _login_rate_limited(key: str) -> bool:
    n = await state_store.incr_window(f"{_LOGIN_RL_PREFIX}{key}", _LOGIN_WINDOW)
    return n > _LOGIN_LIMIT


class LoginRequest(BaseModel):
    username: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str = ""
    token_type: str = "bearer"
    role: str
    user_id: str
    tenant_id: str = "tenant_default"
    expires_in: int = 0   # 访问令牌有效秒数


def _create_token(claims: dict, minutes: int, token_type: str) -> str:
    now = datetime.now(timezone.utc)
    payload = claims.copy()
    payload.update({
        "exp": now + timedelta(minutes=minutes),
        "iat": now,
        "type": token_type,
        "jti": str(uuid.uuid4()),
    })
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


async def _authenticate(username: str, password: str) -> dict | None:
    """DB 用户（bcrypt）优先；无 DB 行回退 mock（离线 demo）。返回含 ver（token_version）。"""
    row = None
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT id, role, tenant_id, password_hash, token_version, is_active "
                "FROM users WHERE username = :u"
            ), {"u": username})).fetchone()
    except Exception:
        row = None
    if row is not None:
        uid, role, tenant, ph, ver, active = row
        if not active:
            return None
        ph = ph or ""
        if ph.startswith("$2"):
            # bcrypt 哈希：恒时校验（bcrypt.checkpw 内部恒时）
            if not verify_password(password, ph):
                return None
        else:
            # 旧占位/明文（init_db 历史 'demo'）：校验后透明升级为 bcrypt，存量免迁移
            if not hmac.compare_digest(ph.encode(), password.encode()):
                return None
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(
                        "UPDATE users SET password_hash = :h WHERE id = :id"
                    ), {"h": hash_password(password), "id": uid})
                logger.info("auth.password_hash_upgraded", user=uid)
            except Exception as e:
                logger.warning("auth.password_hash_upgrade_failed", error=str(e)[:120])
        return {"user_id": uid, "role": role, "tenant_id": tenant or "tenant_default",
                "ver": int(ver or 0)}
    # 回退 mock（离线 demo；无 DB 行故 ver 恒 0）
    m = mock_verify(username, password)
    if m:
        return {**m, "ver": 0}
    return None


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if await _login_rate_limited(f"{req.username}:{client_ip}"):
        raise HTTPException(status_code=429, detail="尝试次数过多，请 1 分钟后再试")
    user = await _authenticate(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    claims = {"sub": user["user_id"], "role": user["role"],
              "tenant_id": user["tenant_id"], "ver": user["ver"]}
    return TokenResponse(
        access_token=_create_token(claims, settings.access_token_minutes, "access"),
        refresh_token=_create_token(claims, settings.refresh_token_days * 24 * 60, "refresh"),
        role=user["role"], user_id=user["user_id"], tenant_id=user["tenant_id"],
        expires_in=settings.access_token_minutes * 60,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_tokens(req: RefreshRequest):
    """刷新令牌换新访问令牌（角色/租户/版本从 DB 重读——角色变更经刷新生效）"""
    exc = HTTPException(status_code=401, detail="无效的刷新令牌")
    try:
        payload = jwt.decode(req.refresh_token, settings.jwt_secret,
                             algorithms=[settings.jwt_algorithm])
    except jwt.InvalidTokenError:
        raise exc
    if payload.get("type") != "refresh":
        raise exc
    if payload.get("jti") and await state_store.in_set(_JTI_SET, payload["jti"]):
        raise exc
    user_id = payload.get("sub")
    if not user_id:
        raise exc
    role = payload.get("role", "user")
    tenant = payload.get("tenant_id", settings.default_tenant_id)
    ver = int(payload.get("ver", 0) or 0)
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT role, tenant_id, token_version, is_active FROM users WHERE id = :i"
            ), {"i": user_id})).fetchone()
        if row is not None:
            if not row[3]:
                raise exc
            # ★ 关键：刷新令牌内嵌的 ver 必须与 DB 一致——否则登出/改密 bump 版本后，
            # 旧刷新令牌仍可换新访问令牌（测试 test_logout_revokes_db_user_all_tokens 捕获过该漏洞）
            if ver != int(row[2] or 0):
                raise exc
            role, tenant, ver = row[0], row[1] or tenant, int(row[2] or 0)
            from backend.dependencies import _VER_CACHE_TTL
            await state_store.set_kv(f"{_VER_PREFIX}{user_id}", str(ver), _VER_CACHE_TTL)
    except HTTPException:
        raise
    except Exception:
        pass   # users 表不可用/mock 用户 → 沿用令牌内声明
    claims = {"sub": str(user_id), "role": role, "tenant_id": tenant, "ver": ver}
    return TokenResponse(
        access_token=_create_token(claims, settings.access_token_minutes, "access"),
        refresh_token=_create_token(claims, settings.refresh_token_days * 24 * 60, "refresh"),
        role=role, user_id=str(user_id), tenant_id=tenant,
        expires_in=settings.access_token_minutes * 60,
    )


@router.post("/logout")
async def logout(current_user: dict = Depends(get_current_user)):
    """登出吊销：jti 拉黑当前令牌；DB 用户 bump token_version（该用户全部令牌失效）"""
    if current_user.get("jti"):
        # TTL 与刷新令牌寿命对齐（超过寿命的 jti 无需再存）
        await state_store.add_set(_JTI_SET, current_user["jti"],
                                  settings.refresh_token_days * 24 * 3600)
    uid = current_user["user_id"]
    bumped = False
    try:
        async with engine.begin() as conn:
            r = await conn.execute(text(
                "UPDATE users SET token_version = token_version + 1 WHERE id = :i"
            ), {"i": uid})
            bumped = bool(r.rowcount)
        await state_store.del_kv(f"{_VER_PREFIX}{uid}")   # 让下次校验直读 DB
    except Exception:
        pass
    return {"status": "revoked", "all_sessions": bumped}


@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return current_user
