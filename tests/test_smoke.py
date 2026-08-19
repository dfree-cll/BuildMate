"""BuildMate Demo 冒烟测试（对标 EduAgent 8.7 端到端测试）
用法：python tests/test_smoke.py
覆盖：登录 → SSE 流式（前置拦截/QA/引导）→ 各 Agent 直达 → 采购 HitL resume
"""
import asyncio
import json
import sys, os

# Windows 控制台 UTF-8 输出
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

BASE = "http://127.0.0.1:8000/api/v1"


async def login(client, username="admin", password="demo123") -> str:
    r = await client.post(BASE + "/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"登录失败: {r.text}"
    token = r.json()["access_token"]
    print(f"✅ 登录成功（{username}）, token:", token[:20] + "...")
    return token


async def stream_chat(client, token, message, session="t1"):
    """消费 SSE 流，收集事件"""
    events = []
    async with client.stream(
        "POST", BASE + "/chat/stream",
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        json={"session_id": session, "message": message}, timeout=180,
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                try:
                    events.append(json.loads(line[5:].strip()))
                except json.JSONDecodeError:
                    pass
    return events


async def main():
    async with httpx.AsyncClient(trust_env=False, timeout=180.0) as client:
        token = await login(client)
        headers = {"Authorization": f"Bearer {token}"}

        # ① 前置拦截：你好
        events = await stream_chat(client, token, "你好")
        types = [e["type"] for e in events]
        assert "token" in types and "done" in types, f"前置拦截事件缺失: {types}"
        print("✅ 规则前置拦截（你好 → 零 Token 模板回复）")

        # ② QA RAG 路由
        events = await stream_chat(client, token, "西安螺纹钢 HRB400 多少钱？")
        types = [e["type"] for e in events]
        assert "routing_decision" in types, f"缺少 routing_decision: {types}"
        rd = [e for e in events if e["type"] == "routing_decision"][0]
        assert rd["agent_type"] == "qa", f"应路由到 qa: {rd}"
        has_answer = any(e["type"] == "token" for e in events)
        print(f"✅ LLM 路由 → qa（{rd['reason']}）；有 token 流: {has_answer}")

        # ③ 投标审查引导
        events = await stream_chat(client, token, "帮我审查投标文件")
        guidances = [e for e in events if e["type"] == "guidance"]
        assert guidances, "应返回 guidance 引导"
        print("✅ 投标审查 → guidance 引导卡片:", guidances[0]["action_label"])

        # ④ 多 Agent pipeline 计划
        events = await stream_chat(client, token, "投标准备一条龙")
        plans = [e for e in events if e["type"] == "pipeline_plan"]
        assert plans, "应返回 pipeline_plan"
        print("✅ multi_agent → pipeline_plan:", plans[0]["title"])

        # ⑤ 投标审查 Agent 直达（orchestrator）
        r = await client.post(BASE + "/agents/bid_review/run",
                             headers=headers, json={"message": "审查某大楼项目投标文件", "session_id": "t2"})
        assert r.status_code == 200, r.text
        j = r.json()
        print("✅ Orchestrator 单 Agent 直达 bid_review, 综合得分:", j["metadata"]["confidence"])

        # ⑥ 采购审批 HitL：小额自动通过
        r = await client.post(BASE + "/procurement/orders",
                             headers=headers,
                             json={"material_name": "砂石", "quantity": 10, "unit_price": 180, "session_id": "t3"})
        j = r.json()
        print("✅ 采购审批（小额自动）:", j["ai_verdict"], "| needs_human:", j["needs_human_approval"])

        # ⑦ 采购审批 HitL：大额 interrupt + 跨用户 resume（买家下单 → 管理员审批，
        #    复现 H1 修复路径：创建与审批不同用户，thread 归属由服务端反查）
        buyer_token = await login(client, "buyer01")
        r = await client.post(BASE + "/procurement/orders",
                             headers={"Authorization": f"Bearer {buyer_token}"},
                             json={"material_name": "螺纹钢", "quantity": 500, "unit_price": 3600, "session_id": "t4"})
        j = r.json()
        assert j["needs_human_approval"] is True, f"大额单应进入人工审批: {j}"
        order_no = j["order_no"]
        print("✅ 采购审批（大额）→ interrupt 人工审批, 单号:", order_no)

        r = await client.post(BASE + f"/procurement/orders/{order_no}/confirm",
                             headers=headers,
                             json={"decision": "approved", "comment": "同意采购"})
        assert r.status_code == 200, r.text
        j = r.json()
        print("✅ HitL resume（买家下单→管理员审批）→ 审批结果:", j["final_verdict"], "|", j["message"][:60])

        # ⑧ 谈判状态机
        r = await client.post(BASE + "/negotiation/chat",
                             headers=headers, json={"message": "开始谈判", "material": "塔吊 QTZ63", "session_id": "t5", "reset": True})
        j = r.json()
        print("✅ 谈判启动, 阶段:", j["stage"], "| 回复:", j["reply"][:50])

        print("\n🎉 全部冒烟测试通过！")


if __name__ == "__main__":
    asyncio.run(main())