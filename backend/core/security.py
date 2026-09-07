"""密码哈希与校验（H4 完整版：DB 用户口令 bcrypt 存储）

成本参数经 settings.bcrypt_rounds 配置（生产 12；测试可经 BCRYPT_ROUNDS 环境变量调低加速）。
"""
import bcrypt

from backend.config import get_settings


def hash_password(password: str) -> str:
    rounds = get_settings().bcrypt_rounds
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # 哈希格式非法（旧占位/脏数据）→ 交给调用方的 legacy 比较分支
        return False
