"""模拟用户（demo 登录校验）

H4（可落地部分）：
- 密码比较改恒时（hmac.compare_digest），消除时序侧信道
- 补充 teacher 角色用户：后端 require_role("admin","teacher") 与前端 TeacherView
  此前只有 admin 能用，teacher 角色名存实亡
企业落地注意：真实部署应切换到 users 表 + bcrypt/argon2 哈希存储（表已建，见 init_db.py），
此处保持 mock 仅用于离线 demo。
"""
import hmac

MOCK_USERS = {
    "admin":     {"user_id": "u-admin",     "role": "admin",   "password": "demo123"},
    "buyer01":   {"user_id": "u-buyer01",   "role": "buyer",   "password": "demo123"},
    "pm01":      {"user_id": "u-pm01",      "role": "project", "password": "demo123"},
    "teacher01": {"user_id": "u-teacher01", "role": "teacher", "password": "demo123"},
}


def mock_verify(username: str, password: str) -> dict | None:
    u = MOCK_USERS.get(username)
    if u and hmac.compare_digest(u["password"], password):
        return {"user_id": u["user_id"], "role": u["role"], "tenant_id": "tenant_default"}
    return None
