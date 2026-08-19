"""可插拔机制回归测试：LLM Provider 注册表 + 向量后端注册表"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatResult, ChatGeneration

from backend.core.llm_factory import (
    LLMFactory, register_llm_provider, _PROVIDER_REGISTRY, _resolve_provider_name,
)
from backend.core.knowledge_base import (
    VectorBackend, register_vector_backend, _BACKENDS, _active_backends,
)


class _EchoProvider(BaseChatModel):
    """测试用自定义 Provider：回显输入（模拟第三方私有化模型接入）"""
    temperature: float = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        text = next((m.content for m in messages if isinstance(m, HumanMessage)), "")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"echo:{text}"))])

    @property
    def _llm_type(self) -> str:
        return "echo-test"


def test_register_custom_llm_provider(monkeypatch):
    """第三方 Provider 注册后经 LLM_PROVIDER 指定即可生效"""
    register_llm_provider("echo-test", lambda agent_type, temperature, streaming: _EchoProvider())
    monkeypatch.setattr("backend.core.llm_factory.get_settings", lambda: type("S", (), {
        "llm_provider": "echo-test", "mock_mode": False,
        "llm_model": "x", "llm_api_key": "k", "llm_base_url": "u"})())
    assert _resolve_provider_name() == "echo-test"
    LLMFactory.clear_cache()
    llm = LLMFactory.get_llm("qa", temperature=0)
    assert isinstance(llm, _EchoProvider)
    LLMFactory.clear_cache()


def test_unknown_llm_provider_raises(monkeypatch):
    monkeypatch.setattr("backend.core.llm_factory.get_settings", lambda: type("S", (), {
        "llm_provider": "nope", "mock_mode": False,
        "llm_model": "x", "llm_api_key": "k", "llm_base_url": "u"})())
    try:
        _resolve_provider_name()
        assert False, "未知 provider 应报错"
    except ValueError as e:
        assert "LLM_PROVIDER" in str(e)


def test_register_custom_vector_backend(monkeypatch):
    """自定义向量后端注册 + VECTOR_BACKEND 指定；local 仍兜底在列"""

    class _StubBackend(VectorBackend):
        name = "stub-test"
        hits = []

        async def add(self, chunks, vectors, tenant_id) -> bool:
            return False

        async def search(self, query, qvec, tenant_id, top_k, min_score):
            self.hits.append(query)
            return [{"content": "stub", "score": 1.0, "dense_score": 1.0, "metadata": {}}]

        async def clear(self, tenant_id) -> None:
            pass

    stub = register_vector_backend(_StubBackend())
    assert _BACKENDS["stub-test"] is stub or isinstance(_BACKENDS["stub-test"], _StubBackend)
    monkeypatch.setattr("backend.core.knowledge_base.get_settings",
                        lambda: type("S", (), {"vector_backend": "stub-test",
                                               "milvus_host": ""})())
    backends = _active_backends()
    names = [b.name for b in backends]
    assert names[0] == "stub-test" and names[-1] == "local", f"自定义后端应优先且 local 兜底: {names}"


async def test_vector_search_via_registered_backend(monkeypatch):
    """注册的后端真实参与检索调度"""
    from backend.core.knowledge_base import search as vs_search, TextVectorizer

    async def _fake_embed(texts):
        return [[0.0] * 256 for _ in texts]
    monkeypatch.setattr(TextVectorizer, "embed", _fake_embed)

    class _FixedBackend(VectorBackend):
        name = "fixed-test"

        async def add(self, chunks, vectors, tenant_id) -> bool:
            return True

        async def search(self, query, qvec, tenant_id, top_k, min_score):
            return [{"content": f"fixed:{query}", "score": 0.9,
                     "dense_score": 0.9, "metadata": {"source_name": "stub"}}]

        async def clear(self, tenant_id) -> None:
            pass

    register_vector_backend(_FixedBackend())
    monkeypatch.setattr("backend.core.knowledge_base.get_settings",
                        lambda: type("S", (), {"vector_backend": "fixed-test",
                                               "milvus_host": ""})())
    results = await vs_search("螺纹钢价格")
    assert results and results[0]["content"] == "fixed:螺纹钢价格"
