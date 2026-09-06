"""Deterministic, heading-aware text chunking."""

from __future__ import annotations

import re

CHUNKING_VERSION = "2.0-heading-1200"


def normalize_text(content: str) -> str:
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_text(content: str, max_chars: int = 1200, overlap: int = 150) -> list[str]:
    """Split deterministically without cutting a paragraph when avoidable."""

    normalized = normalize_text(content)
    if not normalized:
        return []
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", normalized) if item.strip()]
    chunks: list[str] = []
    current = ""
    heading = ""
    for paragraph in paragraphs:
        if re.match(r"^#{1,6}\s+|^第[一二三四五六七八九十百]+[章节条]", paragraph):
            heading = paragraph.split("\n", 1)[0][:200]
        candidate = paragraph if not current else current + "\n\n" + paragraph
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            suffix = current[-overlap:] if overlap else ""
            prefix = heading + "\n\n" if heading and heading not in paragraph else ""
            current = (prefix + suffix + "\n\n" + paragraph).strip()
        else:
            start = 0
            while start < len(paragraph):
                chunks.append(paragraph[start:start + max_chars])
                start += max(1, max_chars - overlap)
            current = ""
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]
