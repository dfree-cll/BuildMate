"""数据库方言辅助（SQLite ↔ PostgreSQL 自适应）
用于生成跨方言的 upsert SQL
"""
from backend.config import get_settings

_settings = get_settings()


def is_postgres() -> bool:
    return "postgresql" in _settings.database_url


def upsert_purchase_order_sql() -> str:
    """采购单 upsert（两方言统一 ON CONFLICT：按 order_no 冲突时只更新可变列，
    保留原 id/created_at —— 旧的 INSERT OR REPLACE 是删除+重插语义，会重置创建时间破坏审计时间线）"""
    if is_postgres():
        return """
            INSERT INTO purchase_orders (id, tenant_id, user_id, order_no, material_name, quantity, unit_price, total_amount, status, ai_result, approved_by)
            VALUES (:id, :tenant_id, :user_id, :order_no, :material_name, :quantity, :unit_price, :total_amount, :status, :ai_result, :approved_by)
            ON CONFLICT (order_no) DO UPDATE SET
              status=excluded.status, ai_result=excluded.ai_result,
              quantity=excluded.quantity, unit_price=excluded.unit_price,
              total_amount=excluded.total_amount, approved_by=excluded.approved_by
        """
    return """
        INSERT INTO purchase_orders (id, tenant_id, user_id, order_no, material_name, quantity, unit_price, total_amount, status, ai_result, approved_by)
        VALUES (:id, :tenant_id, :user_id, :order_no, :material_name, :quantity, :unit_price, :total_amount, :status, :ai_result, :approved_by)
        ON CONFLICT (order_no) DO UPDATE SET
          status=excluded.status, ai_result=excluded.ai_result,
          quantity=excluded.quantity, unit_price=excluded.unit_price,
          total_amount=excluded.total_amount, approved_by=excluded.approved_by
    """


def upsert_qa_session_sql() -> str:
    """qa_sessions upsert"""
    if is_postgres():
        return """
            INSERT INTO qa_sessions (id, tenant_id, user_id, thread_id, summary, summary_version)
            VALUES (:id, :tenant_id, :user_id, :thread_id, :summary, 1)
            ON CONFLICT (thread_id) DO UPDATE SET
              summary = COALESCE(:summary, qa_sessions.summary),
              summary_version = qa_sessions.summary_version + 1,
              updated_at = NOW()
        """
    return """
        INSERT INTO qa_sessions (id, tenant_id, user_id, thread_id, summary, summary_version)
        VALUES (:id, :tenant_id, :user_id, :thread_id, :summary, 1)
        ON CONFLICT (thread_id) DO UPDATE SET
          summary = COALESCE(:summary, qa_sessions.summary),
          summary_version = qa_sessions.summary_version + 1,
          updated_at = CURRENT_TIMESTAMP
    """


def upsert_knowledge_chunk_sql() -> str:
    """knowledge_chunks upsert"""
    if is_postgres():
        return """
            INSERT INTO knowledge_chunks (id, content, vector, source_name, doc_id, chunk_index, tenant_id, updated_at)
            VALUES (:id, :content, :vector, :source_name, :doc_id, :chunk_index, :tenant_id, :updated_at)
            ON CONFLICT (id) DO UPDATE SET content=excluded.content, vector=excluded.vector, updated_at=excluded.updated_at
        """
    return """
        INSERT OR REPLACE INTO knowledge_chunks (id, content, vector, source_name, doc_id, chunk_index, tenant_id, updated_at)
        VALUES (:id, :content, :vector, :source_name, :doc_id, :chunk_index, :tenant_id, :updated_at)
    """
