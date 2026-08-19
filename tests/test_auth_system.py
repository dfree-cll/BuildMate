"""认证体系完整版回归测试（H4 剩余部分）

覆盖：
- bcrypt 哈希/校验
- DB 用户登录（bcrypt）+ 旧占位哈希透明升级
- 双 token：访问/刷新分离（刷新令牌不能当访问令牌用）、/auth/refresh 换新
- 刷新时从 DB 重读角色（角色变更经刷新生效）
- 登出吊销：DB 用户 bump token_version 全端失效；mock 用户走 jti 黑名单
- 停用用户（is_active=0）拒绝登录
"""
import httpx
import pytest
from sqlalchemy import text

from backend.core.security import hash_password, verify_password
from backend.db.session import engine
from backend.main import app
from backend.db.schema import METADATA

BASE = "/api/v1"


@pytest.fixture(autouse=True)
async def _tables():
    async with engine.begin() as conn:
        await conn.run_sync(METADATA.create_all)
    yield


@pytest.fixture
async def api():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        yield c


async def _login(api, username, password):
    r = await api.post(f"{BASE}/auth/login", json={"username": username, "password": password})
    return r


async def _insert_user(uid: str, name: str, role="buyer", pw_hash=None, active=True):
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM users WHERE username = :n"), {"n": name})
        await conn.execute(text(
            "INSERT INTO users (id, tenant_id, username, email, password_hash, role, is_active, token_version)"
            " VALUES (:id, 'tenant_default', :n, :e, :h, :r, :a, 0)"),
            {"id": uid, "n": name, "e": f"{name}@x.local",
             "h": pw_hash or hash_password("secret123"), "r": role, "a": active})


def test_bcrypt_roundtrip():
    h = hash_password("secret123")
    assert h.startswith("$2")
    assert verify_password("secret123", h)
    assert not verify_password("wrong", h)
    assert not verify_password("secret123", "not-a-bcrypt-hash")


async def test_db_user_login_and_me(api):
    await _insert_user("u-auth1", "dbuser01", role="buyer")
    r = await _login(api, "dbuser01", "secret123")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["refresh_token"] and j["expires_in"] > 0
    r = await api.get(f"{BASE}/auth/me", headers={"Authorization": f"Bearer {j['access_token']}"})
    assert r.status_code == 200
    assert r.json()["user_id"] == "u-auth1" and r.json()["role"] == "buyer"


async def test_db_user_wrong_password(api):
    await _insert_user("u-auth2", "dbuser02")
    r = await _login(api, "dbuser02", "wrong-pass")
    assert r.status_code == 401


async def test_legacy_hash_transparent_upgrade(api):
    """存量占位/明文哈希：登录成功后自动升级为 bcrypt"""
    await _insert_user("u-auth3", "legacy01", pw_hash="secret123")   # 旧明文
    r = await _login(api, "legacy01", "secret123")
    assert r.status_code == 200, r.text
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT password_hash FROM users WHERE id = 'u-auth3'"))).fetchone()
    h = row[0]
    assert h.startswith("$2"), "哈希应已透明升级为 bcrypt"


async def test_inactive_user_rejected(api):
    await _insert_user("u-auth4", "disabled01", active=False)
    r = await _login(api, "disabled01", "secret123")
    assert r.status_code == 401


async def test_refresh_flow_and_token_type_separation(api):
    """刷新令牌不能当访问令牌；/auth/refresh 换新后可用"""
    await _insert_user("u-auth5", "refresher01")
    j = (await _login(api, "refresher01", "secret123")).json()
    # 刷新令牌直接访问受保护接口 → 401
    r = await api.get(f"{BASE}/auth/me", headers={"Authorization": f"Bearer {j['refresh_token']}"})
    assert r.status_code == 401
    # 正常刷新
    r = await api.post(f"{BASE}/auth/refresh", json={"refresh_token": j["refresh_token"]})
    assert r.status_code == 200, r.text
    j2 = r.json()
    assert j2["access_token"] != j["access_token"]
    r = await api.get(f"{BASE}/auth/me", headers={"Authorization": f"Bearer {j2['access_token']}"})
    assert r.status_code == 200 and r.json()["user_id"] == "u-auth5"
    # 访问令牌不能当刷新令牌 → 401
    r = await api.post(f"{BASE}/auth/refresh", json={"refresh_token": j["access_token"]})
    assert r.status_code == 401


async def test_refresh_rereads_role_from_db(api):
    """刷新时从 DB 重读角色：改角色 → 刷新 → 新令牌携带新角色"""
    await _insert_user("u-auth6", "promoted01", role="buyer")
    j = (await _login(api, "promoted01", "secret123")).json()
    assert j["role"] == "buyer"
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE users SET role = 'teacher' WHERE id = 'u-auth6'"))
    r = await api.post(f"{BASE}/auth/refresh", json={"refresh_token": j["refresh_token"]})
    assert r.status_code == 200
    assert r.json()["role"] == "teacher"


async def test_logout_revokes_db_user_all_tokens(api):
    """DB 用户登出：bump token_version → 该用户全部令牌立即失效"""
    await _insert_user("u-auth7", "logout01")
    j = (await _login(api, "logout01", "secret123")).json()
    h = {"Authorization": f"Bearer {j['access_token']}"}
    assert (await api.get(f"{BASE}/auth/me", headers=h)).status_code == 200
    r = await api.post(f"{BASE}/auth/logout", headers=h)
    assert r.status_code == 200 and r.json()["all_sessions"] is True
    assert (await api.get(f"{BASE}/auth/me", headers=h)).status_code == 401
    # ★ 刷新令牌同样失效：其内嵌 ver 已落后于 bump 后的 DB 版本（不能换新访问令牌）
    r = await api.post(f"{BASE}/auth/refresh", json={"refresh_token": j["refresh_token"]})
    assert r.status_code == 401


async def test_logout_revokes_mock_user_via_jti(api):
    """mock 用户（无 DB 行）登出：jti 黑名单生效"""
    j = (await _login(api, "admin", "demo123")).json()
    h = {"Authorization": f"Bearer {j['access_token']}"}
    assert (await api.get(f"{BASE}/auth/me", headers=h)).status_code == 200
    r = await api.post(f"{BASE}/auth/logout", headers=h)
    assert r.status_code == 200 and r.json()["all_sessions"] is False
    assert (await api.get(f"{BASE}/auth/me", headers=h)).status_code == 401
