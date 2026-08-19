"""从公开数据网抓取建筑行业真实知识内容（政策法规/规范条文）
数据源（免费、公开、权威，已全部验证可抓）：
1. 首都之窗/北京市政府网：招标投标法实施条例全文
2. 中国政府网 gov.cn 公报：建设工程质量管理条例、建设工程安全生产管理条例
输出：data/knowledge_real/ 下的 markdown 文件
"""
import re
import sys
from pathlib import Path

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import requests

_UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0'}
OUT_DIR = Path(__file__).parent.parent / "data" / "knowledge_real"

_CONTENT_IDS = ('UCAP-CONTENT', 'zoom', 'vsb_content', 'article-content')
_CONTENT_CLASS = re.compile(
    r'<div[^>]*class="[^"]*(?:TRS_Editor|TRS_Editor_1|wzcon|pages_content|article-content|detail_content)[^"]*"[^>]*>(.*?)</div>',
    re.S,
)
_NAV_NOISE = re.compile(
    r'^(首页|收藏|取消收藏|打印|字号|大|中|小|默认|超大|政务公开|政策服务|政策文件|其他文件|分享到|国务院公报|增刊\d+|\|?\s*$|[\[][^\]]*[\]]|--+\s*$)'
)


def _extract_body(html: str) -> str:
    for cid in _CONTENT_IDS:
        m = re.search(r'<div[^>]*id="' + cid + r'"[^>]*>(.*?)</div>s*(?:</div>|<script|<div)', html, re.S)
        if m and len(m.group(1)) > 200:
            return m.group(1)
    m = _CONTENT_CLASS.search(html)
    if m and len(m.group(1)) > 200:
        return m.group(1)
    return html


def fetch_policy(url: str) -> str:
    r = requests.get(url, headers=_UA, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or 'utf-8'
    t = re.sub(r'<script.*?</script>', '', r.text, flags=re.S)
    t = re.sub(r'<style.*?</style>', '', t, flags=re.S)
    t = _extract_body(t)
    body = re.sub(r'<[^>]+>', '\n', t)
    for a, b in [('&nbsp;', ' '), ('&ensp;', ' '), ('&emsp;', ' '),
                 ('&ldquo;', '"'), ('&rdquo;', '"'), ('&lsquo;', "'"), ('&rsquo;', "'"),
                 ('&mdash;', '-'), ('&ndash;', '-'), ('&middot;', '·'), ('&times;', '×')]:
        body = body.replace(a, b)
    body = re.sub(r'&[a-zA-Z#0-9]+;', '', body)
    lines = []
    for l in body.split('\n'):
        s = l.strip()
        if not s or _NAV_NOISE.match(s):
            continue
        lines.append(s)
    dedup = []
    for l in lines:
        if not dedup or dedup[-1] != l:
            dedup.append(l)
    return '\n'.join(dedup)


SOURCES = [
    {
        "name": "招标投标法实施条例",
        "file": "招投标政策-招标投标法实施条例.md",
        "url": "https://www.beijing.gov.cn/zhengce/zhengcefagui/qtwj/201202/t20120201_780989.html",
        "title": "# 招投标政策\n\n## 招标投标法实施条例（国务院令第613号）\n\n> 来源：北京市人民政府门户网站（公开政策文件全文）",
    },
    {
        "name": "建设工程质量管理条例",
        "file": "工程质量-建设工程质量管理条例.md",
        "url": "https://www.gov.cn/gongbao/content/2019/content_5468867.htm",
        "title": "# 工程质量\n\n## 建设工程质量管理条例（2019年修正）\n\n> 来源：中国政府网国务院公报（公开全文）",
    },
    {
        "name": "建设工程安全生产管理条例",
        "file": "施工安全-建设工程安全生产管理条例.md",
        "url": "https://www.gov.cn/gongbao/content/2004/content_63050.htm",
        "title": "# 施工安全\n\n## 建设工程安全生产管理条例（国务院令第393号）\n\n> 来源：中国政府网国务院公报（公开全文）",
    },
]


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for src in SOURCES:
        print(f"抓取：{src['name']} ...")
        try:
            text = fetch_policy(src["url"])
            lines = text.split('\n')
            # 跳过正文开头的文件名/导航残留
            while lines and (len(lines[0]) < 15 or '国务院公报' in lines[0] or '政务' in lines[0]):
                lines = lines[1:]
            content = src["title"] + "\n\n" + '\n'.join(lines)
            fp = OUT_DIR / src["file"]
            fp.write_text(content, encoding="utf-8")
            print(f"  OK {fp.name} ({len(content)} 字符)")
        except Exception as e:
            print(f"  FAIL {src['name']}: {type(e).__name__} {str(e)[:120]}")

    print(f"\n完成，输出目录：{OUT_DIR}")


if __name__ == "__main__":
    main()
