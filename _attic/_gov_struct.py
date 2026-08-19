# -*- coding: utf-8 -*-
import requests, sys, re
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0'}
r = requests.get('https://www.gov.cn/gongbao/content/2019/content_5468867.htm', headers=UA, timeout=15)
r.encoding = r.apparent_encoding or 'utf-8'
# 找"第一章"前后的 HTML
idx = r.text.find('第一章')
print('第一章 at:', idx)
if idx > 0:
    seg = r.text[max(0,idx-1500):idx+500]
    # 找最近的 div 开始
    divs = re.findall(r'<div[^>]*>', seg)
    print('divs in seg:', divs[-6:])
    print('---')
    print(seg[:1200])
