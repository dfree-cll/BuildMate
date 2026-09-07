"""建材价格爬虫（对标 SmartVoyage spider_weather.py 的数据接入模式）
功能：
1. fetch：从 百年建筑网（水泥/混凝土/砂石 现货）+ 新浪期货（螺纹钢/热卷/线材/铁矿/铜 主连）抓真实价格
2. should_update：24 小时内不重复抓取
3. store：写入 material_prices 表（SQLite/PG 跨方言 upsert）
4. scheduler：每日定时更新（--daemon）

用法：
  python scripts/fetch_material_prices.py            # 需要更新则抓取一次
  python scripts/fetch_material_prices.py --force    # 强制抓取一次
  python scripts/fetch_material_prices.py --daemon   # 启动定时任务（每天 08:30）
  python scripts/fetch_material_prices.py --query 水泥   # 查询已入库价格
"""
import argparse
import asyncio
import sys
import os

# Windows 控制台 UTF-8 输出
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.core import material_prices as mp


async def cmd_update(force: bool):
    print("🕷️  建材价格爬虫启动（对标 spider_weather.py）...")
    # ② 更新策略（对标 spider_weather.should_update）：24h 内不重复抓取
    if not await mp.should_update(force):
        print("⏭️  数据已为最新（24 小时内），跳过抓取。可用 --force 强制更新。")
        return
    # ① 抓取
    print("① 抓取百年建筑网（水泥/混凝土/砂石 现货区间价）...")
    bainian = mp.fetch_bainian_prices()
    print(f"   → {len(bainian)} 条")
    print("② 抓取新浪期货（螺纹钢/热卷/线材/铁矿/铜 主力合约）...")
    sina = mp.fetch_sina_futures()
    print(f"   → {len(sina)} 条")
    all_rows = mp.clean_price_rows(bainian + sina)   # 清洗管线：归一/校验/去重

    # ③ 清空快照后入库（价格表为最新快照语义）
    from sqlalchemy import text as _text
    from backend.db.session import engine as _engine
    if all_rows:
        async with _engine.begin() as conn:
            await conn.execute(_text("DELETE FROM material_prices"))
    stored = await mp.store_prices(all_rows)
    print(f"③ 清洗后入库：{stored}/{len(all_rows)} 条（material_prices 表）")

    # ④ 预览
    latest = await mp.latest_prices()
    print("\n📊 当前真实价格数据：")
    print(mp.format_price_text(latest) or "  （无）")


async def cmd_query(keyword: str):
    rows = await mp.query_prices(keyword)
    if not rows:
        print(f"未找到含「{keyword}」的价格记录。")
        return
    print(f"找到 {len(rows)} 条：")
    print(mp.format_price_text(rows))


async def cmd_daemon():
    print("⏰ 定时任务启动：每天 08:30 自动更新建材价格（Ctrl+C 退出）")
    try:
        import schedule
    except ImportError:
        print("  未安装 schedule，仅执行一次后退出。")
        await cmd_update(force=False)
        return
    schedule.every().day.at("08:30").do(lambda: asyncio.run(cmd_update(force=True)))
    while True:
        schedule.run_pending()
        await asyncio.sleep(60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="建材价格爬虫")
    parser.add_argument("--force", action="store_true", help="强制抓取（忽略 24h 缓存）")
    parser.add_argument("--daemon", action="store_true", help="启动定时任务")
    parser.add_argument("--query", metavar="关键词", help="查询已入库价格")
    args = parser.parse_args()

    if args.query:
        asyncio.run(cmd_query(args.query))
    elif args.daemon:
        asyncio.run(cmd_daemon())
    else:
        asyncio.run(cmd_update(force=args.force))
