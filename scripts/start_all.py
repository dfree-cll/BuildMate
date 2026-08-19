"""一键启动 BuildMate Demo
用法：python scripts/start_all.py
"""
import subprocess
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    python = os.path.join(root, ".venv", "Scripts", "python.exe")
    if not os.path.exists(python):
        python = sys.executable

    print("① 初始化数据库...")
    subprocess.run([python, "scripts/init_db.py"], cwd=root, check=True)

    print("② 灌入知识库（RAG）...")
    subprocess.run([python, "scripts/seed_knowledge.py"], cwd=root, check=True)

    print("②.5 抓取真实建材价格（material_prices，失败不阻塞）...")
    try:
        subprocess.run([python, "scripts/fetch_material_prices.py", "--force"], cwd=root, timeout=120)
    except Exception as e:
        print(f"  ⚠️ 价格抓取跳过：{e}")

    print("③ 启动 FastAPI (http://localhost:8000)...")
    # 经 run_backend.py 启动：Windows+PG 必须 Selector 事件循环（直接 -m uvicorn 会因
    # psycopg 不支持 Proactor 在 lifespan 无限重试后失败）。参数列表形式，无 shell。
    subprocess.run(
        [r"F:\BuildMate\BuildMateDemo\.venv\Scripts\python.exe", "run_backend.py"],
        cwd=root,
        check=False,
    )


if __name__ == "__main__":
    main()