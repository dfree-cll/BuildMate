"""谈判 Agent 单元测试（状态机推进/报告生成）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.agents.negotiation.nodes import check_stage_node, init_node
from backend.agents.negotiation.state import NegotiationStage, STAGE_ORDER


async def test_init_sets_quote_stage():
    """新会话初始化为报价阶段"""
    st = await init_node({})
    assert st["stage"] == NegotiationStage.QUOTE.value
    assert st["stage_index"] == 0


async def test_init_preserves_stage_on_resume():
    """续谈时保留当前阶段"""
    st = await init_node({"stage": "tech", "stage_index": 1})
    assert st == {}, "已有阶段不应重置"


async def test_check_stage_advances():
    """对话轮数足够时推进阶段"""
    from langchain_core.messages import HumanMessage, AIMessage
    msgs = [HumanMessage(content="hi"), AIMessage(content="a"),
            HumanMessage(content="hello"), AIMessage(content="b")]
    st = await check_stage_node({
        "stage": "quote", "stage_index": 0, "messages": msgs,
    })
    assert st.get("stage") != "quote", "应推进到下一阶段"


async def test_stage_order_complete():
    """阶段顺序完整：quote→tech→delivery→sign→done"""
    assert [s.value for s in STAGE_ORDER] == ["quote", "tech", "delivery", "sign", "done"]
