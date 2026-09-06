"""环境自检（CI 与本地共用；PyCharm 直接运行亦可）
检查：必需依赖 / 可选依赖（本地大模型）/ 本地模型目录 / LLM key / 数据库连通 / 知识库文件

修复要点（2026-08-19）：
- 所有路径锚定项目根（此前相对 CWD：PyCharm 工作目录不同时，.env/知识库被误报缺失）
- 依赖清单与 requirements.txt 对齐：移除已弃用的 jose，补 pyjwt/bcrypt/alembic/redis/mcp 等
- torch/transformers/sentence-transformers 降为"可选"（缺失只降级语义检索，不算失败）
- 数据库检查只做连接握手（engine.connect 完成协议握手即证明可达，无 SQL 语句）
- 必需项缺失时以退出码 1 结束（CI 可感知）；可选项缺失仅告警
"""
import asyncio
import importlib
import os
import sys
from pathlib import Path

# Windows PowerShell commonly exposes a GBK stream while the diagnostics use
# status symbols.  Reconfigure only the process-local streams so the same
# checker works from a terminal, PyCharm and CI without changing user locale.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)   # 锚定 CWD：config 的 .env 与 data/ 检查均相对项目根

# 必需依赖（import 名）：缺失 = 环境不可用（CI 失败）
REQUIRED_DEPS = [
    "fastapi", "uvicorn", "langgraph", "sqlalchemy", "aiosqlite",
    "pydantic_settings", "jwt", "bcrypt",            # jwt = PyJWT（jose 已弃用）
    "alembic", "redis", "httpx", "sse_starlette",
    "pymupdf",                                         # PDF 解析
    "ifcopenshell",                                    # IFC BIM 解析
    "mcp", "duckduckgo_search",                        # MCP 双 Server
]
# 可选依赖：本地语义模型栈，缺失自动降级（哈希向量/跳过精排），不阻塞
OPTIONAL_DEPS = ["torch", "transformers", "sentence_transformers"]


def check(ok: bool, name: str, detail: str = "") -> bool:
    mark = "✅" if ok else "❌"
    print(f"{mark} {name}" + (f" | {detail}" if detail else ""))
    return ok


def main():
    print("=== BuildMate 环境自检 ===\n")
    all_ok = True

    # ① Python 版本
    py = sys.version_info
    all_ok &= check(py >= (3, 11), "Python 3.11+", f"{py.major}.{py.minor}")

    # ② 必需依赖
    for d in REQUIRED_DEPS:
        try:
            importlib.import_module(d)
            check(True, f"依赖 {d}")
        except ImportError:
            all_ok &= check(False, f"依赖 {d}", "MISSING")

    # ③ 可选依赖（本地语义模型；缺失降级，不算失败）
    for d in OPTIONAL_DEPS:
        try:
            importlib.import_module(d)
            check(True, f"可选依赖 {d}")
        except ImportError:
            print(f"⚠️ 可选依赖 {d} 未安装（语义检索降级为哈希向量，功能可用）")

    # ④ 本地模型目录（BGE-M3/Reranker/分类器；缺失自动降级，不算失败）
    from backend.config import get_settings
    settings = get_settings()
    for m in ["embedding/bge-m3", "reranker/bge-reranker-large", "classifier/query-classifier-finetuned"]:
        p = Path(settings.models_root) / m
        if p.is_dir():
            check(True, f"模型 {m}")
        else:
            print(f"⚠️ 模型 {m} 未找到（将自动降级为哈希向量/规则分类）")

    # ⑤ LLM key
    all_ok &= check(bool(settings.llm_api_key), "LLM API Key",
                    "真实模式" if settings.llm_api_key else "Mock 模式（未配 key）")

    # ⑥ 数据库连通（仅握手；同循环内 dispose 防 Windows 跨事件循环关闭连接的噪音）
    db_ok = True
    try:
        asyncio.run(_test_db())
    except Exception as e:
        db_ok = False
        print(f"   数据库错误：{str(e)[:120]}")
    all_ok &= check(db_ok, "数据库连接", settings.database_url)

    # ⑦ 知识库文件（锚定项目根）
    kb_files = list((ROOT / "data" / "knowledge").glob("*.md")) + \
               list((ROOT / "data" / "knowledge_real").glob("*.md"))
    all_ok &= check(bool(kb_files), "知识库文件", f"{len(kb_files)} 个 md")

    print("\n=== 结论 ===")
    print("环境就绪 ✅" if all_ok else "环境有问题，见上方 ❌ 项")
    sys.exit(0 if all_ok else 1)


async def _test_db():
    from backend.db.session import engine
    async with engine.connect():
        pass                       # 握手成功即视为连通
    await engine.dispose()


if __name__ == "__main__":
    main()
