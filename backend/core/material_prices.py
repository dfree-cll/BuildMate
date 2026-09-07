"""建材价格服务：真实数据爬取 + 存储 + 查询（对标 spider_weather.py 的数据接入模式）

数据源（均免费、无需 API Key）：
1. 百年建筑网 https://www.100njz.com —— 水泥/混凝土/砂石 现货市场区间价（品名/市场/价格区间/日期）
2. 新浪财经期货 hq.sinajs.cn（nf_ 前缀）—— 螺纹钢/热卷/线材/铁矿石/铜 主力合约最新价

存储：SQLite / PostgreSQL 通用表 material_prices（跨方言）
"""
import asyncio
import re
import sys
import uuid
from datetime import datetime, date

import requests
from sqlalchemy import text

from backend.core.logger import get_logger
from backend.db.session import engine
from backend.db.dialect import is_postgres

logger = get_logger(__name__)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── 请求头 ─────────────────────────────────────────────
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "Chrome/120.0.0.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA}
_SINA_HEADERS = {"User-Agent": _UA, "Referer": "https://finance.sina.com.cn"}

# ═══════════════ 数据清洗管线（标准化 → 校验 → 归一 → 去重）═══════════════
# 材料别名归一：同义词 → 标准名（供 RAG 检索与展示统一）
_MATERIAL_ALIASES = {
    "螺纹钢": ("螺纹钢", "钢筋", "HRB400", "HRB400E", "HRB500", "盘螺", "螺纹"),
    "热卷": ("热卷", "热轧卷板", "热轧板卷"),
    "线材": ("线材", "盘条", "高线"),
    "混凝土": ("混凝土", "商砼", "商品混凝土", "C30", "C25", "C35", "C40"),
    "砂石": ("砂石", "河砂", "机制砂", "碎石", "中砂", "粗砂"),
    "水泥": ("水泥", "P.O42.5", "P.O 42.5", "P.C32.5", "散装水泥", "袋装水泥"),
    "铁矿石": ("铁矿石", "铁矿", "矿石"),
    "铜": ("铜", "沪铜", "电解铜"),
}
_ALIAS_LOOKUP = {alias: std for std, aliases in _MATERIAL_ALIASES.items() for alias in aliases}

# 价格合理性上下限（元/单位，防脏数据：0 值 / 天文数字 / 负值）
_PRICE_LIMITS = {
    "螺纹钢": (1000, 20000), "热卷": (1000, 20000), "线材": (1000, 20000),
    "铁矿石": (100, 5000), "铜": (10000, 300000), "水泥": (50, 3000),
    "混凝土": (50, 3000), "砂石": (10, 2000), "矿渣粉": (50, 2000),
}
_DEFAULT_PRICE_LIMIT = (1, 1000000)

_UNIT_NORMALIZE = {"元/立方米": "元/方", "元/m³": "元/方", "元/m3": "元/方", "元/吨": "元/吨", "元/方": "元/方", "元/平方米": "元/平米"}


def _normalize_material(name: str) -> str:
    """材料同义词 → 标准名（优先精确匹配，其次包含匹配）"""
    name = (name or "").strip().upper()
    for alias, std in _ALIAS_LOOKUP.items():
        if alias.upper() == name:
            return std
    # 包含匹配（如"螺纹钢主连" → 螺纹钢）
    for alias, std in _ALIAS_LOOKUP.items():
        if alias.upper() in name:
            return std
    return name


def _normalize_date(d: str) -> str:
    """日期补全年份：MM-DD → YYYY-MM-DD（跨年：日期晚于今天则视为去年）"""
    d = (d or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return d
    m = re.match(r"^(\d{2})-(\d{2})$", d)
    if not m:
        return d
    mm, dd = m.groups()
    today = date.today()
    year = today.year
    if (int(mm), int(dd)) > (today.month, today.day):
        year -= 1  # 数据日期在未来（跨年场景，如 12 月数据 1 月补录）→ 视为去年
    return f"{year}-{mm}-{dd}"


def _normalize_unit(u: str) -> str:
    return _UNIT_NORMALIZE.get((u or "").strip(), u or "元/吨")


def _validate_price(material: str, low: float, high: float) -> bool:
    """价格合理性校验：非负、low<=high、在材料合理区间内"""
    if low <= 0 or high < low:
        return False
    lo, hi = _PRICE_LIMITS.get(_normalize_material(material), _DEFAULT_PRICE_LIMIT)
    return lo <= low and high <= hi


def clean_price_rows(rows: list[dict]) -> list[dict]:
    """统一清洗管线：材料归一 → 单位归一 → 日期补全 → 价格校验 → 去重
    返回清洗后的行列表（含 cleaned/dropped 统计可经 logger 查看）"""
    cleaned: list[dict] = []
    seen: set[tuple] = set()
    dropped = 0
    for row in rows:
        r = dict(row)
        r["material"] = _normalize_material(r.get("material", ""))
        r["unit"] = _normalize_unit(r.get("unit", "元/吨"))
        r["price_date"] = _normalize_date(r.get("price_date", ""))
        try:
            low = float(r.get("price_low", 0)); high = float(r.get("price_high", 0))
        except (TypeError, ValueError):
            dropped += 1
            continue
        if not _validate_price(r["material"], low, high):
            logger.warning("spider.clean_dropped", material=r.get("material"),
                           low=low, high=high, reason="price_out_of_range")
            dropped += 1
            continue
        r["price_low"], r["price_high"] = low, high
        # 去重键：material+spec+market+source+date
        key = (r["material"], r.get("spec", ""), r.get("market", ""), r.get("source", ""), r.get("price_date", ""))
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        cleaned.append(r)
    if dropped:
        logger.info("spider.clean_summary", kept=len(cleaned), dropped=dropped)
    return cleaned


# ── 百年建筑网频道 ─────────────────────────────────────
_BAINIAN_CHANNELS = [
    ("水泥", "https://www.100njz.com/cement/", "元/吨"),
    ("混凝土", "https://www.100njz.com/concrete/", "元/方"),
    ("砂石", "https://www.100njz.com/aggregate/", "元/方"),
]

# ── 新浪期货品种（主力连续合约）────────────────────────
_SINA_FUTURES = [
    ("螺纹钢", "nf_RB0"),
    ("热卷", "nf_HC0"),
    ("线材", "nf_WR0"),
    ("铁矿石", "nf_I0"),
    ("铜", "nf_CU0"),
]

def upsert_material_price_sql() -> str:
    """material_prices upsert（SQLite: INSERT OR REPLACE；PG: ON CONFLICT）"""
    if is_postgres():
        return """
            INSERT INTO material_prices (id, material, spec, market, price_low, price_high, unit, price_date, source, updated_at)
            VALUES (:id, :material, :spec, :market, :price_low, :price_high, :unit, :price_date, :source, NOW())
            ON CONFLICT (material, spec, market, source) DO UPDATE SET
              price_low=excluded.price_low, price_high=excluded.price_high,
              unit=excluded.unit, price_date=excluded.price_date, updated_at=NOW()
        """
    return """
        INSERT OR REPLACE INTO material_prices (id, material, spec, market, price_low, price_high, unit, price_date, source, updated_at)
        VALUES (:id, :material, :spec, :market, :price_low, :price_high, :unit, :price_date, :source, CURRENT_TIMESTAMP)
    """


# ═══════════════ ① 抓取：百年建筑网 ═══════════════
def fetch_bainian_prices() -> list[dict]:
    """抓百年建筑网频道页价格表 → [{material, spec, market, price_low, price_high, unit, price_date, source}]"""
    rows: list[dict] = []
    for material, url, unit in _BAINIAN_CHANNELS:
        try:
            r = requests.get(url, headers=_HEADERS, timeout=15)
            r.raise_for_status()
            html = r.content.decode("utf-8", errors="replace")
            # 表格行：<td class="t3">品名</td> <td class="t2">市场</td> <td class="t3">价格区间</td> <td class="t2">日期</td>
            pattern = re.compile(
                r'<td class="t3">([^<]+)</td>\s*<td class="t2">([^<]+)</td>\s*'
                r'<td class="t3">\s*([\d.\s\-～~—]+)\s*</td>\s*<td class="t2">\s*([\d-]+)\s*</td>',
                re.S,
            )
            found = 0
            for m in pattern.finditer(html):
                name, market, price_txt, price_date = [x.strip() for x in m.groups()]
                if not re.search(r"\d", price_txt):
                    continue
                lows = re.findall(r"(\d+(?:\.\d+)?)", price_txt)
                if not lows:
                    continue
                nums = [float(x) for x in lows]
                rows.append({
                    "material": material, "spec": name if name != material else "",
                    "market": market, "price_low": min(nums), "price_high": max(nums),
                    "unit": unit, "price_date": price_date, "source": "百年建筑网",
                })
                found += 1
            logger.info("spider.bainian", channel=material, url=url, found=found)
        except Exception as e:
            logger.warning("spider.bainian_failed", channel=material, error=str(e)[:120])
    return rows


# ═══════════════ ② 抓取：新浪期货 ═══════════════
def fetch_sina_futures() -> list[dict]:
    """抓新浪财经主力合约行情 → [{material, spec, market, price_low, price_high, unit, price_date, source}]"""
    rows: list[dict] = []
    codes = ",".join(code for _, code in _SINA_FUTURES)
    try:
        r = requests.get(f"https://hq.sinajs.cn/list={codes}", headers=_SINA_HEADERS, timeout=10)
        r.raise_for_status()
        for line in r.text.split(";"):
            line = line.strip()
            if not line.startswith("var hq_str_nf_") or '=""' in line:
                continue
            try:
                payload = line.split('="', 1)[1].rsplit('"', 1)[0]
                f = payload.split(",")
                if len(f) < 18:
                    continue
                # 字段：0名称 1时间 2开 3高 4低 5昨收 6买 7卖 8最新 9结算 10昨结算 ... 15交易所 16品种 17日期
                latest = float(f[8]) if f[8] else 0.0
                if latest <= 0:
                    latest = float(f[2]) if f[2] else 0.0
                price_date = f[17] if len(f) > 17 and re.match(r"^\d{4}-\d{2}-\d{2}$", f[17]) else date.today().isoformat()
                # f[16] 为中文品种名（如"螺纹钢"），f[0] 为"螺纹钢连续"
                material = f[16].replace("连续", "").strip() or f[0].replace("连续", "").strip()
                rows.append({
                    "material": material, "spec": "主力合约",
                    "market": "全国（期货）", "price_low": latest, "price_high": latest,
                    "unit": "元/吨", "price_date": price_date, "source": "新浪财经期货",
                })
            except Exception:
                continue
        logger.info("spider.sina", codes=codes, found=len(rows))
    except Exception as e:
        logger.warning("spider.sina_failed", error=str(e)[:120])
    return rows


# ═══════════════ ③ 存储 ═══════════════
async def store_prices(rows: list[dict]) -> int:
    """批量 upsert 到 material_prices，返回成功条数"""
    if not rows:
        return 0
    sql = upsert_material_price_sql()
    ok = 0
    async with engine.begin() as conn:
        for row in rows:
            try:
                await conn.execute(text(sql), {
                    "id": str(uuid.uuid4()),
                    "material": row["material"],
                    "spec": row.get("spec", ""),
                    "market": row["market"],
                    "price_low": row["price_low"],
                    "price_high": row["price_high"],
                    "unit": row.get("unit", "元/吨"),
                    "price_date": row.get("price_date"),
                    "source": row.get("source", "未知"),
                })
                ok += 1
            except Exception as e:
                logger.warning("spider.store_row_failed", material=row.get("material"), error=str(e)[:100])
    return ok


# ═══════════════ ④ 查询 ═══════════════
async def query_prices(material_hint: str, market_hint: str = "") -> list[dict]:
    """按材料（模糊）+ 市场（模糊）查最新价格记录（按更新时间倒序）"""
    async with engine.connect() as conn:
        result = await conn.execute(text("""
            SELECT material, spec, market, price_low, price_high, unit, price_date, source, updated_at
            FROM material_prices
            WHERE material LIKE :m
              AND (:mk = '' OR market LIKE :mk)
            ORDER BY updated_at DESC, price_date DESC
        """), {"m": f"%{material_hint}%", "mk": market_hint})
        return [dict(zip(("material", "spec", "market", "price_low", "price_high", "unit", "price_date", "source", "updated_at"), row))
                for row in result.fetchall()]


async def latest_prices() -> list[dict]:
    """全部最新价格（每个 material+market 取最近一条）"""
    async with engine.connect() as conn:
        result = await conn.execute(text("""
            SELECT material, spec, market, price_low, price_high, unit, price_date, source, updated_at
            FROM material_prices
            ORDER BY material, market, updated_at DESC
        """))
        rows = [dict(zip(("material", "spec", "market", "price_low", "price_high", "unit", "price_date", "source", "updated_at"), r))
                for r in result.fetchall()]
    seen, out = set(), []
    for row in rows:
        key = (row["material"], row["market"])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def format_price_text(rows: list[dict]) -> str:
    """把价格记录格式化为自然语言（供 QA 注入）"""
    if not rows:
        return ""
    lines = []
    for r in rows:
        if r["price_low"] == r["price_high"]:
            price_str = f"{r['price_low']:g}"
        else:
            price_str = f"{r['price_low']:g} ~ {r['price_high']:g}"
        spec = f"（{r['spec']}）" if r.get("spec") else ""
        lines.append(
            f"- {r['material']}{spec} {r['market']}：{price_str} {r['unit']}，"
            f"数据日期 {r['price_date']}，来源 {r['source']}"
        )
    return "\n".join(lines)


# ═══════════════ ⑤ 更新策略（对标 spider_weather.should_update）═══════════════
async def should_update(force: bool = False) -> bool:
    """表为空 / 最新更新时间超过 24 小时 → 需要更新"""
    if force:
        return True
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT MAX(updated_at) FROM material_prices"))).fetchone()
        latest = row[0] if row else None
        if not latest:
            return True
        if isinstance(latest, str):
            latest_dt = datetime.fromisoformat(latest.replace("T", " "))
        else:
            latest_dt = latest.replace(tzinfo=None)
        return (datetime.now() - latest_dt).total_seconds() / 3600 >= 24
    except Exception as e:
        logger.warning("spider.should_update_failed", error=str(e)[:80])
        return True


async def update_prices(force: bool = False) -> dict:
    """完整更新流程：抓取（线程池，M3：同步 requests 不再阻塞事件循环）→ 清空+入库（单事务原子快照）→ 汇总"""
    if not await should_update(force):
        return {"skipped": True, "rows": 0, "reason": "数据已为最新（24 小时内）"}
    raw_rows = await asyncio.to_thread(fetch_bainian_prices) \
        + await asyncio.to_thread(fetch_sina_futures)
    rows = clean_price_rows(raw_rows)   # 清洗管线：归一/校验/去重
    ok = 0
    if rows:
        sql = upsert_material_price_sql()
        try:
            # M3：快照替换收敛到单个事务（旧实现 DELETE 与 INSERT 分两个事务，
            # 中间崩溃会把价格表清空）；任一行失败整体回滚，保留旧快照
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM material_prices"))
                for row in rows:
                    await conn.execute(text(sql), {
                        "id": str(uuid.uuid4()),
                        "material": row["material"],
                        "spec": row.get("spec", ""),
                        "market": row["market"],
                        "price_low": row["price_low"],
                        "price_high": row["price_high"],
                        "unit": row.get("unit", "元/吨"),
                        "price_date": row.get("price_date"),
                        "source": row.get("source", "未知"),
                    })
                    ok += 1
        except Exception as e:
            logger.warning("spider.snapshot_swap_failed", kept_old=True, error=str(e)[:150])
            return {"skipped": False, "rows": len(rows), "stored": 0,
                    "error": str(e)[:150],
                    "materials": sorted({r["material"] for r in rows})}
        logger.info("spider.snapshot_swapped", cleared=len(rows))
    return {"skipped": False, "rows": len(rows), "stored": ok,
            "materials": sorted({r["material"] for r in rows})}


if __name__ == "__main__":
    asyncio.run(update_prices(force=True))
