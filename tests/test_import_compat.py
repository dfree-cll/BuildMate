"""中间件中台 façade 兼容性测试

断言：
1. 旧路径（backend.core.* / backend.mcp.client）所有公开符号仍可导入——防止 re-export 遗漏
2. 新门面导出的符号与旧路径是同一对象（is 相等）——防止门面指向错误实现
"""
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

# (旧模块路径, 符号名) —— 覆盖五大门面的全部导出
LEGACY_EXPORTS = {
    "backend.core.knowledge_base": [
        "DIM", "TextVectorizer", "VectorBackend", "LocalSQLiteBackend", "MilvusBackend",
        "register_vector_backend", "cosine_sim", "add_chunks", "search", "clear_knowledge",
    ],
    "backend.core.reranker": ["BGEReranker", "rerank_results"],
    "backend.core.memory": [
        "init_memory_savers", "get_memory_saver", "close_memory_savers",
        "build_thread_id", "build_config", "trim_messages_to_window",
        "should_trigger_summary", "compress_to_summary",
    ],
    "backend.core.llm_factory": [
        "register_llm_provider", "MockChatModel", "LLMFactory", "get_llm", "get_structured_llm",
    ],
    "backend.mcp.client": ["TOOL_REGISTRY", "DEFAULT_TOOL_ACL", "call_mcp_tool", "check_tool_access"],
    "backend.core.observability": ["ensure_llm_calls_table", "record_llm_call", "get_call_stats"],
}

def test_legacy_paths_importable():
    """旧路径所有公开符号仍可导入（防止 re-export 改动破坏存量调用）"""
    for module_path, symbols in LEGACY_EXPORTS.items():
        mod = importlib.import_module(module_path)
        for sym in symbols:
            assert hasattr(mod, sym), f"{module_path}.{sym} 导入失败"


def test_public_rag_exports_are_real_symbols():
    """检查真实的 RAG 门面导出，不把模块自身与自身比较造成恒真断言。"""
    facade = importlib.import_module("backend.rag")
    exports = getattr(facade, "__all__", [])
    assert exports
    for symbol in exports:
        assert hasattr(facade, symbol), f"backend.rag.__all__ 声明了不存在的符号 {symbol}"


def test_legacy_knowledge_base_vectorizer_points_to_rag_canonical():
    """存量导入路径必须指向 RAG 唯一实现，而不是保留第二份算法。"""
    legacy = importlib.import_module("backend.core.knowledge_base")
    canonical = importlib.import_module("backend.rag.vectorization")
    assert legacy.TextVectorizer is canonical.TextVectorizer
    assert legacy.cosine_sim is canonical.cosine_sim
    assert legacy._sparse_bm25 is canonical._sparse_bm25


def test_legacy_reranker_points_to_rag_canonical():
    legacy = importlib.import_module("backend.core.reranker")
    canonical = importlib.import_module("backend.rag.reranking")
    assert legacy.BGEReranker is canonical.BGEReranker
    assert legacy.rerank_results is canonical.rerank_results


def test_agents_package_does_not_eagerly_load_ifc_dependency():
    """Importing an Agent package must stay lightweight and lazy-load IFC."""
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, backend.agents; "
            "raise SystemExit(1 if 'ifcopenshell' in sys.modules else 0)",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr or probe.stdout


def test_v2_event_stream_cursor_is_shared_and_monotonic():
    from backend.api.v2.event_stream import normalize_event_cursor

    assert normalize_event_cursor("8", 3) == 8
    assert normalize_event_cursor("2", 3) == 3
    assert normalize_event_cursor("not-a-number", 3) == 3
