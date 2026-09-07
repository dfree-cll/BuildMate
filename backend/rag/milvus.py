"""Project-aware dense index for the v2 knowledge pipeline."""

from __future__ import annotations

import json

from backend.config import get_settings


class MilvusKnowledgeIndex:
    COLLECTION = "buildmate_knowledge_v2"
    DIM = 1024

    def __init__(self) -> None:
        from pymilvus import DataType, MilvusClient

        settings = get_settings()
        self.client = MilvusClient(uri=f"http://{settings.milvus_host}:{settings.milvus_port}")
        if not self.client.has_collection(self.COLLECTION):
            schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
            schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=self.DIM)
            schema.add_field("tenant_id", DataType.VARCHAR, max_length=64)
            schema.add_field("project_id", DataType.VARCHAR, max_length=64)
            schema.add_field("scope", DataType.VARCHAR, max_length=16)
            schema.add_field("document_id", DataType.VARCHAR, max_length=64)
            schema.add_field("source_name", DataType.VARCHAR, max_length=256)
            schema.add_field("page_no", DataType.INT64)
            schema.add_field("metadata_json", DataType.VARCHAR, max_length=4096)
            indexes = self.client.prepare_index_params()
            indexes.add_index(
                field_name="embedding", index_type="HNSW", metric_type="COSINE",
                params={"M": 16, "efConstruction": 256},
            )
            self.client.create_collection(
                collection_name=self.COLLECTION, schema=schema, index_params=indexes
            )

    def upsert(
        self,
        *,
        tenant_id: str,
        project_id: str | None,
        scope: str,
        document_id: str,
        source_name: str,
        metadata: dict,
        chunks: list[dict],
    ) -> None:
        rows = []
        for chunk in chunks:
            vector = json.loads(chunk["vector"])
            if len(vector) != self.DIM:
                raise ValueError(f"Milvus requires {self.DIM}-dimension embeddings")
            rows.append({
                "id": chunk["id"], "embedding": vector,
                "tenant_id": tenant_id[:64], "project_id": (project_id or "")[:64],
                "scope": scope[:16], "document_id": document_id[:64],
                "source_name": source_name[:256], "page_no": int(chunk.get("page_no") or 0),
                "metadata_json": json.dumps(metadata, ensure_ascii=False)[:4096],
            })
        if rows:
            self.client.upsert(collection_name=self.COLLECTION, data=rows)

    def search(
        self,
        vector: list[float],
        *,
        tenant_id: str,
        project_id: str | None,
        scope: str,
        limit: int,
    ) -> dict[str, float]:
        if len(vector) != self.DIM:
            raise ValueError(f"Milvus requires {self.DIM}-dimension embeddings")
        tenant = _escape(tenant_id)
        project = _escape(project_id or "")
        if scope == "global":
            expression = f'tenant_id == "{tenant}" and scope == "global"'
        elif scope == "tenant":
            expression = f'tenant_id == "{tenant}" and scope in ["global", "tenant"]'
        else:
            expression = (
                f'tenant_id == "{tenant}" and '
                f'(scope in ["global", "tenant"] or (scope == "project" and project_id == "{project}"))'
            )
        result = self.client.search(
            collection_name=self.COLLECTION,
            data=[vector],
            filter=expression,
            limit=max(1, min(limit, 100)),
            output_fields=["id"],
            search_params={"metric_type": "COSINE", "params": {"ef": 128}},
        )
        return {
            str(item.get("id")): max(0.0, float(item.get("distance", 0.0)))
            for item in (result[0] if result else [])
        }


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
