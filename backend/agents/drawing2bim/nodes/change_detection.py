"""drawing2bim 变更检测（架构图 B 节点：比对历史哈希）

策略：对图纸提取文本做 SHA-256 内容哈希，与 state_store 中保存的历史哈希比对：
- 无历史 → "first"（全量感知）
- 哈希不同 → "changed"（局部感知/重感知）
- 哈希相同 → "unchanged"（跳过重感知，直接复用基准）

第一期简化：变更区域圈定（仅框定变更区域）留待多模态感知期实现，
本期检测到变更后整体重感知。
"""
import hashlib

from backend.core.logger import get_logger
from backend.core.state_store import state_store

logger = get_logger(__name__)

_HASH_KEY_PREFIX = "d2b:drawing_hash:"
# 图纸哈希保留 30 天（跨会话变更检测）；到期后视同首次感知
_HASH_TTL_S = 30 * 24 * 3600


def compute_content_hash(text: str) -> str:
    """图纸文本内容哈希（空白归一化后计算，避免无意义差异）"""
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def detect_change(review_key: str, content_hash: str) -> str:
    """比对历史哈希，返回 "first" / "changed" / "unchanged"

    review_key: 图纸标识（同一图纸多轮审查用同一 key，如文件名或会话内 ID）
    """
    stored = await state_store.get_kv(_HASH_KEY_PREFIX + review_key)
    if stored is None:
        return "first"
    return "unchanged" if stored == content_hash else "changed"


async def save_content_hash(review_key: str, content_hash: str) -> None:
    """保存本次哈希（供下轮变更检测）"""
    await state_store.set_kv(_HASH_KEY_PREFIX + review_key, content_hash, _HASH_TTL_S)
