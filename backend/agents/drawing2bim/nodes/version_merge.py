"""drawing2bim 版本合并器（架构图 F 节点：叠写至完整 JSON）

按 element_id 将增量基准叠写（upsert）到既有黄金基准：
- 同 element_id：新记录覆盖旧记录（source 标记为 merged，保留更高置信度一方）
- 新 element_id：追加
- 既有记录不在增量中：保留（增量不删除，删除需显式指令，留待后续）
"""
from backend.core.logger import get_logger

logger = get_logger(__name__)


def merge_baseline(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """纯函数：增量叠写合并（便于单测）"""
    merged = {e.get("element_id"): dict(e) for e in existing if e.get("element_id")}
    added = updated = 0

    for inc in incoming:
        eid = inc.get("element_id")
        if not eid:
            continue
        if eid in merged:
            old = merged[eid]
            # 置信度低的一方不覆盖关键属性（保守合并）
            if inc.get("confidence", 0) >= old.get("confidence", 0):
                merged[eid] = {**inc, "source": "merged"}
            else:
                merged[eid] = {**old, "source": "merged"}
            updated += 1
        else:
            merged[eid] = {**inc, "source": "merged"}
            added += 1

    logger.info("version_merge.done", added=added, updated=updated,
                total=len(merged))
    return list(merged.values())
