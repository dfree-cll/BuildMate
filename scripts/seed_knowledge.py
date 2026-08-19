"""灌入知识库文档（RAG 用）— 对齐行业范式-03 智能分块（MarkdownHeaderTextSplitter + MarkdownTextSplitter 两阶段）
扫描目录：
  data/knowledge      模拟/示例知识库
  data/knowledge_real 公开数据网抓取的真实知识库（法规/规范）
用法：python scripts/seed_knowledge.py
"""
import asyncio
import sys, os
from pathlib import Path

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, MarkdownTextSplitter

from backend.core.knowledge_base import add_chunks, clear_knowledge
from backend.config import get_settings

_MD_HEADER_SPLITTER = MarkdownHeaderTextSplitter(
    headers_to_split_on=[("#", "H1"), ("##", "H2"), ("###", "H3"), ("####", "H4")],
    strip_headers=False,  # 保留标题行，chunk 自带上下文
)


def split_markdown_documents(text: str, source: str, chunk_size: int = 1200,
                             chunk_overlap: int = 100) -> list[dict]:
    """对标行业范式-03：按标题语义切分 + 超长块二次切分"""
    splitter = MarkdownTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    header_chunks = _MD_HEADER_SPLITTER.split_text(text)
    for c in header_chunks:
        c.metadata["source"] = source
    final_chunks = splitter.split_documents(header_chunks)

    out = []
    for i, chunk in enumerate(final_chunks):
        filename = Path(source).stem if source else "未知文件"
        parts = [chunk.metadata.get(k, "") for k in ("H1", "H2", "H3", "H4")]
        parts = [p for p in parts if p]
        source_name = f"{filename} > {' > '.join(parts)}" if parts else filename
        out.append({
            "content": chunk.page_content,
            "source_name": source_name,
            "doc_id": filename,
            "chunk_index": i,
        })
    return out


async def main():
    settings = get_settings()
    await clear_knowledge(settings.default_tenant_id)
    dirs = [
        Path(__file__).parent.parent / "data" / "knowledge",
        Path(__file__).parent.parent / "data" / "knowledge_real",
    ]
    total = 0
    for kb_dir in dirs:
        if not kb_dir.exists():
            continue
        for fp in sorted(kb_dir.glob("*.md")):
            text = fp.read_text(encoding="utf-8")
            chunks = split_markdown_documents(text, fp.name)
            await add_chunks(chunks, tenant_id=settings.default_tenant_id)
            total += len(chunks)
            print(f"  {fp.name}: {len(chunks)} chunks")
    print(f"✅ 知识库灌入完成，共 {total} 个 chunk")


if __name__ == "__main__":
    asyncio.run(main())
