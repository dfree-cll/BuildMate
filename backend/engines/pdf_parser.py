"""文档解析工具
投标文件解析：PDF（含内嵌图片 OCR）/ Word（docx）/ 图片（jpg/png）/ 纯文本
PDF 支持双栏布局；保留 extract_pdf_text 旧接口兼容。
"""
import asyncio
import os

# OCR 开关：OCR_ENABLED=0 可关（证照图片提取慢时用）；OCR_USE_GPU=0 强制 CPU（默认自动 GPU）
_OCR_ENABLED = os.environ.get("OCR_ENABLED", "1") != "0"
_OCR_USE_GPU = os.environ.get("OCR_USE_GPU", "1") != "0"
# 页面视为"扫描页"的文本阈值（少于该字符数 → 整页渲染 OCR）
_SCAN_PAGE_TEXT_THRESHOLD = 30
# 单页最多 OCR 的图片数（防装饰性图片拖慢）
_MAX_OCR_IMAGES_PER_PAGE = 3
# 单份文档最多 OCR 页数（扫描件全 OCR 太慢；覆盖大部分标书内容）
_MAX_OCR_PAGES = 30
# OCR 前图片缩放宽度（onnxruntime 对超大图慢；表格小字需要保留细节，2000 折中精度与速度）
_OCR_MAX_WIDTH = 2000

_ocr_engine = None


def _get_ocr():
    """懒加载 RapidOCR 单例（CPU onnxruntime；GPU 版暂不可用——CUDA 运行库未完整，见 onnxruntime 1.19 需 cuDNN 9）"""
    global _ocr_engine
    if _ocr_engine is None and _OCR_ENABLED:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _ocr_engine = RapidOCR()
        except ImportError:
            _ocr_engine = False  # 引擎不可用 → 永久跳过 OCR（不重复尝试）
    return _ocr_engine or None


def _ocr_image_bytes(img_bytes: bytes) -> str:
    """OCR 识别图片字节 → 文本（失败返回空）"""
    engine = _get_ocr()
    if not engine:
        return ""
    try:
        import numpy as np
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        # 缩放提速：onnxruntime 对超大图极慢，宽度缩到 _OCR_MAX_WIDTH（保持比例）
        if img.width > _OCR_MAX_WIDTH:
            ratio = _OCR_MAX_WIDTH / img.width
            img = img.resize((_OCR_MAX_WIDTH, max(1, int(img.height * ratio))), Image.LANCZOS)
        result, _ = engine(np.array(img))
        if not result:
            return ""
        lines = [line[1] for line in result if len(line) > 1]
        return "\n".join(lines)
    except Exception:
        return ""


def _sync_extract_pdf(pdf_path: str) -> dict:
    """同步 PDF 文本提取（PyMuPDF，双栏检测；在线程池中运行）"""
    import pymupdf as fitz          # 1.24+ 新命名（旧 fitz 名已弃用）
    from pymupdf import utils

    doc = fitz.open(pdf_path)
    page_count = len(doc)
    all_text_parts = []
    ocr_parts = []

    for page in doc:
        blocks = utils.get_text(page, "blocks")
        text_blocks = [b for b in blocks if b[6] == 0]
        page_text = ""
        if text_blocks:
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

        # ── OCR 通道：有内嵌图片的页 / 几乎无文本的扫描页（全文档最多 _MAX_OCR_PAGES 页）──
        if not _OCR_ENABLED or len(ocr_parts) >= _MAX_OCR_PAGES:
            continue
        page_ocr_lines = []
        # ① 内嵌图片对象（营业执照/身份证通常是图片对象）
        try:
            for img_info in page.get_images(full=True)[:_MAX_OCR_IMAGES_PER_PAGE]:
                xref = img_info[0]
                pix = fitz.Pixmap(doc, xref)
                if pix.width < 100 or pix.height < 100:   # 跳过小图标/装饰
                    continue
                img_bytes = pix.tobytes("png")
                text = _ocr_image_bytes(img_bytes)
                if text.strip():
                    page_ocr_lines.append(text)
        except Exception:
            pass
        # ② 扫描页：整页渲染 OCR
        if len(page_text.strip()) < _SCAN_PAGE_TEXT_THRESHOLD and not page_ocr_lines:
            try:
                pix = page.get_pixmap(dpi=150)
                text = _ocr_image_bytes(pix.tobytes("png"))
                if text.strip():
                    page_ocr_lines.append(text)
            except Exception:
                pass
        if page_ocr_lines:
            ocr_parts.append("【图片OCR】" + "\n".join(page_ocr_lines))

    doc.close()
    raw_text = "\n\n---PAGE BREAK---\n\n".join(all_text_parts)
    if ocr_parts:
        raw_text += "\n\n---OCR CONTENT---\n\n" + "\n\n".join(ocr_parts)
    return {"raw_text": raw_text, "page_count": page_count}


async def extract_pdf_text(pdf_path: str) -> dict:
    """异步 PDF 解析入口（线程池，不阻塞事件循环）"""
    return await asyncio.to_thread(_sync_extract_pdf, pdf_path)


# ═══════════════ 统一文档解析（PDF / Word / 图片 / 文本）═══════════════

_SUPPORTED_EXTS = {".pdf", ".docx", ".jpg", ".jpeg", ".png", ".bmp", ".txt", ".md"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}


def _sync_parse_docx(docx_path: str) -> dict:
    """Word 解析：段落 + 表格 + 内嵌图片 OCR（营业执照/社保证明等常为图片）"""
    from docx import Document
    doc = Document(docx_path)
    paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    table_lines = []
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                table_lines.append(" | ".join(cells))
    raw_text = "\n".join(paras)
    if table_lines:
        raw_text += "\n\n【表格内容】\n" + "\n".join(table_lines)

    # 内嵌图片 OCR（证照/证明类图片，最多 15 张防拖慢）
    if _OCR_ENABLED:
        ocr_parts = []
        for rel in doc.part.rels.values():
            if "image" in rel.reltype and len(ocr_parts) < 15:
                try:
                    img_bytes = rel.target_part.blob
                    t = _ocr_image_bytes(img_bytes)
                    if t.strip():
                        ocr_parts.append("【图片OCR】" + t)
                except Exception:
                    continue
        if ocr_parts:
            raw_text += "\n\n---OCR CONTENT---\n\n" + "\n\n".join(ocr_parts)
    return {"raw_text": raw_text, "page_count": 0}


def _sync_parse_image(image_path: str) -> dict:
    """图片直接 OCR（营业执照/身份证单独上传）"""
    with open(image_path, "rb") as f:
        img_bytes = f.read()
    text = _ocr_image_bytes(img_bytes)
    raw_text = f"【图片OCR】\n{text}" if text.strip() else "（图片无法识别出文字）"
    return {"raw_text": raw_text, "page_count": 0}


def _sync_parse_document(path: str, filename: str) -> dict:
    """统一解析分发：按扩展名（保留原名用于大小写）"""
    ext = os.path.splitext(filename or path)[1].lower()
    if ext not in _SUPPORTED_EXTS:
        raise ValueError(f"不支持的格式：{ext}（支持 PDF / Word / 图片 / txt）")
    if ext == ".pdf":
        return _sync_extract_pdf(path)
    if ext == ".docx":
        return _sync_parse_docx(path)
    if ext in _IMAGE_EXTS:
        return _sync_parse_image(path)
    # .txt / .md
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return {"raw_text": f.read(), "page_count": 0}


async def parse_document(path: str, filename: str) -> dict:
    """异步统一解析入口（线程池）"""
    return await asyncio.to_thread(_sync_parse_document, path, filename)
