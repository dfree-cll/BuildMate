# -*- coding: utf-8 -*-
"""抓取 GB51251 建标库真实条文 → data/knowledge_real/消防排烟-GB51251.md"""
import requests, sys, re
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0'}
BASE = 'http://www.jianbiaoku.com'

# 章节列表（标题, url）
CHAPTERS = [
    ('总则与术语', '/webarbs/book/120622/3732422.shtml'),
    ('防烟系统设计', '/webarbs/book/120622/3732427.shtml'),
    ('排烟系统设计', '/webarbs/book/120622/3732432.shtml'),
    ('防烟分区', '/webarbs/book/120622/3732434.shtml'),
    ('自然排烟设施', '/webarbs/book/120622/3732435.shtml'),
    ('机械排烟设施', '/webarbs/book/120622/3732436.shtml'),
    ('排烟系统设计计算', '/webarbs/book/120622/3732438.shtml'),
    ('系统控制-排烟', '/webarbs/book/120622/3732441.shtml'),
]

def fetch_chapter(url):
    r = requests.get(BASE + url, headers=UA, timeout=15)
    r.encoding = r.apparent_encoding or 'utf-8'
    t = re.sub(r'<script.*?</script>', '', r.text, flags=re.S)
    t = re.sub(r'<style.*?</style>', '', t, flags=re.S)
    # 建标库正文容器
    m = re.search(r'<div[^>]*(?:class|id)="[^"]*(?:text|content|article|detail)[^"]*"[^>]*>(.*?)</div>', t, re.S)
    body = m.group(1) if m else t
    body = re.sub(r'<[^>]+>', '\n', body)
    for a, b in [('&nbsp;', ' '), ('&ensp;', ' '), ('&emsp;', ' '), ('&ldquo;', '"'), ('&rdquo;', '"')]:
        body = body.replace(a, b)
    body = re.sub(r'&[a-zA-Z#0-9]+;', '', body)
    lines = [l.strip() for l in body.split('\n') if l.strip() and len(l.strip()) > 1]
    return '\n'.join(lines)

OUT = Path(__file__).parent.parent / "data" / "knowledge_real"
OUT.mkdir(parents=True, exist_ok=True)
content = "# 消防排烟\n\n## 建筑防烟排烟系统技术标准（GB 51251-2017）\n\n> 来源：建标库（公开条文全文）\n\n"
total = 0
for title, url in CHAPTERS:
    try:
        text = fetch_chapter(url)
        content += f"\n### {title}\n\n{text}\n"
        total += len(text)
        print(f"  OK {title}: {len(text)} 字符")
    except Exception as e:
        print(f"  FAIL {title}: {type(e).__name__} {str(e)[:80]}")

fp = OUT / "消防排烟-GB51251建筑防烟排烟系统技术标准.md"
fp.write_text(content, encoding='utf-8')
print(f"\n✅ {fp.name} 共 {total} 字符")
