"""本地开发后端启动器（Windows 必经入口；容器/Linux 不受影响）

为什么存在：Windows 上 uvicorn 0.36+ 默认用 Proactor 事件循环，而 psycopg
（PostgresSaver 检查点）只支持 Selector 循环——直接 `python -m uvicorn`
会在 lifespan 建 PG 连接池时无限重试后启动失败。
解决：经 uvicorn 的 loop="module:factory" 官方扩展点注入 Selector 工厂。

用法：
  python run_backend.py            # 127.0.0.1:8000
  python run_backend.py 9000       # 指定端口
  python run_backend.py --reload   # Windows 下热重载与 psycopg 冲突，自动降级并提示
"""
import asyncio
import sys


def selector_loop_factory(**kwargs):
    """强制 Selector 事件循环（psycopg 兼容）；uvicorn 会以 use_subprocess=... 调用"""
    return asyncio.SelectorEventLoop()


def main() -> None:
    import uvicorn

    args = sys.argv[1:]
    port = 8000
    for a in args:
        if a.isdigit():
            port = int(a)

    run_kwargs: dict = {"host": "127.0.0.1", "port": port}
    if sys.platform == "win32":
        # 关键：覆盖 uvicorn 默认的 ProactorEventLoop 工厂（官方 loop 导入串扩展点）
        run_kwargs["loop"] = "run_backend:selector_loop_factory"
        if "--reload" in args:
            print("⚠️  Windows + PostgreSQL 不支持热重载（reload 子进程会重建 Proactor 循环），已降级为普通模式")
            print("   开发建议：改用 SQLite（清空 DATABASE_URL）后可用 --reload；或改完手动重启")
    elif "--reload" in args:
        run_kwargs["reload"] = True

    uvicorn.run("backend.main:app", **run_kwargs)


if __name__ == "__main__":
    main()
