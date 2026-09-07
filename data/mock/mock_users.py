"""模拟用户（demo 登录校验）

离线 Demo 账号与数据库种子保持一致；密码比较使用恒时比较，消除时序侧信道。
企业落地注意：真实部署应切换到 users 表 + bcrypt/argon2 哈希存储（表已建，见 init_db.py），
此处保持 mock 仅用于离线 demo。
"""
import hmac

MOCK_USERS = {
    "admin":     {"user_id": "u-admin",     "role": "admin",   "password": "demo123"},
    "buyer01":   {"user_id": "u-buyer01",   "role": "buyer",   "password": "demo123"},
    "pm01":      {"user_id": "u-pm01",      "role": "project", "password": "demo123"},
    "reviewer01": {"user_id": "u-reviewer01", "role": "reviewer", "password": "demo123"},
}


def mock_verify(username: str, password: str) -> dict | None:
    u = MOCK_USERS.get(username)
    if u and hmac.compare_digest(u["password"], password):
        return {"user_id": u["user_id"], "role": u["role"], "tenant_id": "tenant_default"}
    return None
