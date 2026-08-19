"""PDF 解析工具（对标 EduAgent 4.4 简历 PDF 解析：双栏检测 + 线程池）
用于投标文件 PDF 解析，支持双栏布局
"""
import asyncio


def _sync_extract_pdf(pdf_path: str) -> dict:
    """同步 PDF 文本提取（PyMuPDF，双栏检测；在线程池中运行）"""
    import pymupdf as fitz          # 1.24+ 新命名（旧 fitz 名已弃用）
    from pymupdf import utils

    doc = fitz.open(pdf_path)
    page_count = len(doc)
    all_text_parts = []

    for page in doc:
        blocks = utils.get_text(page, "blocks")
        text_blocks = [b for b in blocks if b[6] == 0]
        if not text_blocks:
            continue

        page_width = page.rect.width
        midpoint = page_width / 2
        left_blocks = [b for b in text_blocks if b[2] < midpoint + 20]
        right_blocks = [b for b in text_blocks if b[0] >= midpoint - 20]
        is_two_column = (
            len(left_blocks) >= 2 and len(right_blocks) >= 2
            and len(right_blocks) / max(len(text_blocks), 1) > 0.3
        )

        if is_two_column:
            left_sorted = sorted(left_blocks, key=lambda b: b[1])
            right_sorted = sorted(right_blocks, key=lambda b: b[1])
            page_text = ("\n".join(b[4].strip() for b in left_sorted if b[4].strip()) + "\n"
                         + "\n".join(b[4].strip() for b in right_sorted if b[4].strip()))
        else:
            sorted_blocks = sorted(text_blocks, key=lambda b: b[1])
            page_text = "\n".join(b[4].strip() for b in sorted_blocks if b[4].strip())

        if page_text.strip():
            all_text_parts.append(page_text)

    doc.close()
    raw_text = "\n\n---PAGE BREAK---\n\n".join(all_text_parts)
    return {"raw_text": raw_text, "page_count": page_count}


async def extract_pdf_text(pdf_path: str) -> dict:
    """异步 PDF 解析入口（线程池，不阻塞事件循环）"""
    return await asyncio.to_thread(_sync_extract_pdf, pdf_path)
