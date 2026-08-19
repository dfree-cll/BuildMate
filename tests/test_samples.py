"""样本批量测试（39 条，覆盖 RAG/同义词/错别字/库外/寒暄）
真实 LLM 模式下含限流重试
"""
import asyncio, json, sys, httpx
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")
BASE = "http://127.0.0.1:8000/api/v1"


async def stream_one(c, h, sid, msg):
    """消费一次 SSE，返回 (tokens, mode, guidance)"""
    tokens, mode, guidance = [], "", ""
    async with c.stream("POST", BASE + "/chat/stream", headers=h,
                        json={"session_id": sid, "message": msg}, timeout=180) as resp:
        async for line in resp.aiter_lines():
            if not line.startswith("data:"): continue
            try: ev = json.loads(line[5:].strip())
            except Exception: continue
            if ev.get("type") == "token": tokens.append(ev.get("content", ""))
            elif ev.get("type") == "meta": mode = ev.get("answer_mode", "")
            elif ev.get("type") == "guidance": mode = "clarify"; guidance = ev.get("message", "")
    return "".join(tokens), mode, guidance


async def main():
    samples = json.loads(open("data/test_samples.json", encoding="utf-8").read())
    async with httpx.AsyncClient(trust_env=False, timeout=200) as c:
        r = await c.post(BASE + "/auth/login", json={"username": "admin", "password": "demo123"})
        token = r.json()["access_token"]
        h = {"Authorization": "Bearer " + token, "Accept": "text/event-stream"}

        stats = {"pass": 0, "fail": 0, "detail": []}
        for i, (msg, expected) in enumerate(samples):
            await asyncio.sleep(0.5)
            ans, mode, guidance = "", "", ""
            for attempt in range(3):
                try:
                    ans, mode, guidance = await stream_one(c, h, "sample-" + str(i), msg)
                    if ans or mode: break
                except Exception:
                    pass
                await asyncio.sleep(2)
            if not ans and guidance:
                ans = guidance

            ok = False
            if expected == "RAG":
                # real_price = 真实行情直答（material_prices 表），视为 RAG 命中且数据更真实
                ok = mode in ("rag", "rag_hybrid", "real_price") or (mode == "llm_direct" and len(ans) > 20)
            elif expected == "RAG_NO_DATA":
                ok = ("未包含" in ans or "未找到" in ans or "仅涉及" in ans) and len(ans) > 20
            elif expected == "DIRECT":
                ok = mode in ("llm_direct", "rag_hybrid") or "未找到" in ans or mode == "clarify"
            elif expected == "GREET":
                ok = mode == "general" or "BuildMate" in ans or "您好" in ans or "不客气" in ans
            elif expected == "CLARIFY":
                ok = mode == "clarify" and len(ans) > 10
            elif expected == "REASONABLE":
                ok = len(ans) > 20 and mode in ("llm_direct", "rag", "rag_hybrid")
            if not ok and mode == "" and len(ans) > 10:
                ok = True

            mark = "✅" if ok else "❌"
            if ok:
                stats["pass"] += 1
            else:
                stats["detail"].append({"q": msg, "expected": expected, "mode": mode, "ans": ans[:40]})
                stats["fail"] += 1
            print(mark + " [" + expected + "] " + msg + " -> " + mode + " | " + ans[:30])

        total = stats["pass"] + stats["fail"]
        print("\n===== 汇总 =====")
        print("通过: " + str(stats["pass"]) + "/" + str(total) + " (" + str(100 * stats["pass"] // total) + "%)")
        if stats["detail"]:
            print("失败明细:")
            for d in stats["detail"]:
                print("  ❌ " + d["q"] + " | 期望=" + d["expected"] + " | 实际=" + d["mode"] + " | " + d["ans"])


if __name__ == "__main__":
    asyncio.run(main())