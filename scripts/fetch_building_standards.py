"""通用建标库规范抓取器
用法：python scripts/fetch_building_standards.py [规范名]
不传参则抓取 _STANDARDS 清单中的全部规范
"""
import re
import sys
from pathlib import Path

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import requests

_UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0'}
_BASE = 'http://www.jianbiaoku.com'
OUT_DIR = Path(__file__).parent.parent / "data" / "knowledge_real"

# ── 常用建筑规范清单（书ID 从建标库详情页获取）──
# 名称, 书ID, 标准编号标题
_STANDARDS = [
    ("GB 50016-2014(2018年版) 建筑设计防火规范", 56160),
    ("GB 50010-2010(2024年版) 混凝土结构设计规范", 209),
    ("GB 50007-2011 建筑地基基础设计规范", 237),
    ("GB 50202-2018 建筑地基基础工程施工质量验收标准", 1029),
    ("GB 50300-2013 建筑工程施工质量验收统一标准", 1028),
    ("GB 50011-2010(2016年版) 建筑抗震设计规范", 276),
]


def get_book_links(book_id: int) -> list[tuple[str, str]]:
    """从书详情页提取章节链接 (标题, url)"""
    url = f"{_BASE}/webarbs/book/{book_id}.shtml"
    r = requests.get(url, headers=_UA, timeout=20)
    r.encoding = r.apparent_encoding or 'utf-8'
    links = re.findall(r'href=["\']([^"\']*webarbs/book/[^"\']*)["\'][^>]*>([^<]{2,50})<', r.text)
    chapters = []
    seen = set()
    for u, t in links:
        t = t.strip()
        if not t or t in seen:
            continue
        # 排除非章节（相关推荐/其他书）
        if f'/book/{book_id}/' not in u:
            continue
        seen.add(t)
        chapters.append((t, u))
    return chapters


def fetch_chapter(url: str) -> str:
    r = requests.get(_BASE + url, headers=_UA, timeout=20)
    r.encoding = r.apparent_encoding or 'utf-8'
    t = re.sub(r'<script.*?</script>', '', r.text, flags=re.S)
    t = re.sub(r'<style.*?</style>', '', t, flags=re.S)
    m = re.search(r'<div[^>]*(?:class|id)="[^"]*(?:text|content|article|detail)[^"]*"[^>]*>(.*?)</div>', t, re.S)
    body = m.group(1) if m else t
    body = re.sub(r'<[^>]+>', '\n', body)
    for a, b in [('&nbsp;', ' '), ('&ensp;', ' '), ('&emsp;', ' '), ('&ldquo;', '"'), ('&rdquo;', '"'),
                 ('&mdash;', '-'), ('&middot;', '·'), ('&times;', '×'), ('&divide;', '÷')]:
        body = body.replace(a, b)
    body = re.sub(r'&[a-zA-Z#0-9]+;', '', body)
    lines = [l.strip() for l in body.split('\n') if l.strip() and len(l.strip()) > 1]
    return '\n'.join(lines)


def fetch_standard(title: str, book_id: int) -> str:
    """抓取一个规范 → markdown 文本"""
    chapters = get_book_links(book_id)
    print(f"  {title}: 发现 {len(chapters)} 个章节")
    # 过滤：只保留正文章节（排除 相关推荐/其他规范 等）
    skip_kw = ('相关推荐', '上一篇', '下一篇', '咨询', '购买', '下载', '评论', '会员', 'VIP')
    chapters = [(t, u) for t, u in chapters if not any(k in t for k in skip_kw)]
    content = f"# {title.split(' ')[0]} {title.split(' ')[1] if len(title.split(' '))>1 else ''}\n\n## {title}\n\n> 来源：建标库（公开条文全文）\n\n"
    total = 0
    for ctitle, curl in chapters[:30]:  # 最多 30 章
        try:
            text = fetch_chapter(curl)
            if len(text) < 50:
                continue
            content += f"\n### {ctitle}\n\n{text}\n"
            total += len(text)
        except Exception as e:
            print(f"    FAIL {ctitle}: {str(e)[:60]}")
    return content, total


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    targets = sys.argv[1:]
    for title, book_id in _STANDARDS:
        if targets and not any(t in title for t in targets):
            continue
        if book_id <= 0:
            print(f"  SKIP {title}: 书ID 未确认")
            continue
        print(f"抓取：{title}")
        try:
            content, total = fetch_standard(title, book_id)
            fname = re.sub(r'[^\w\-]+', '_', title)[:50]
            fp = OUT_DIR / f"{fname}.md"
            fp.write_text(content, encoding='utf-8')
            print(f"  ✅ {fp.name} ({total} 字符)")
        except Exception as e:
            print(f"  ❌ {title}: {type(e).__name__} {str(e)[:100]}")


if __name__ == "__main__":
    main()
