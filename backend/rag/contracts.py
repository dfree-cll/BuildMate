"""Versioned RAG contracts shared by agents, API and MCP."""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class KnowledgeScope(str, enum.Enum):
    GLOBAL = "global"
    TENANT = "tenant"
    PROJECT = "project"


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=2000)
    tenant_id: str = Field(..., min_length=1, max_length=64)
    project_id: str | None = Field(default=None, max_length=64)
    scope: KnowledgeScope = KnowledgeScope.TENANT
    top_k: int = Field(default=8, ge=1, le=30)
    filters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _project_scope_requires_project(self) -> "KnowledgeSearchRequest":
        if self.scope == KnowledgeScope.PROJECT and not self.project_id:
            raise ValueError("project_id is required for project scope")
        return self


class KnowledgeHit(BaseModel):
    chunk_id: str
    content: str
    source_name: str = ""
    document_id: str
    page_no: int | None = Field(default=None, ge=1)
    score: float
    dense_score: float = 0.0
    sparse_score: float = 0.0
    rerank_score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class Citation(BaseModel):
    chunk_id: str
    document_id: str
    source_name: str = ""
    page_no: int | None = None


class RAGAnswer(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    grounded: bool = False
    abstained: bool = False
    retrieval_run_id: str


class KnowledgeDocumentCreate(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    scope: KnowledgeScope = KnowledgeScope.TENANT
    source_type: str = Field(..., min_length=1, max_length=32)
    source_uri: str = Field(..., min_length=1, max_length=2048)
    title: str = Field(..., min_length=1, max_length=256)
    document_version: str = Field(default="1", min_length=1, max_length=32)
    content: str = Field(..., min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_scope(self) -> "KnowledgeDocumentCreate":
        if self.scope == KnowledgeScope.PROJECT and not self.project_id:
            raise ValueError("project_id is required for project documents")
        if self.scope != KnowledgeScope.PROJECT and self.project_id is not None:
            raise ValueError("project_id is only valid for project documents")
        return self
