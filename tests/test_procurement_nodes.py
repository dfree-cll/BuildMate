"""采购审批 Agent 单元测试（规则引擎/合并/HitL 小额分支）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.agents.procurement.nodes import _run_rule_engine, merge_node, human_in_the_loop_node


def test_rule_engine_marks_large_order():
    """大额采购触发 review"""
    st = _run_rule_engine({
        "total_amount": 500000, "unit_price": 3600, "quantity": 100,
    })
    assert st["verdict"] == "review"
    assert any("大额" in i for i in st["issues"])


def test_rule_engine_passes_small_order():
    """小额采购 pass"""
    st = _run_rule_engine({
        "total_amount": 1000, "unit_price": 100, "quantity": 10,
    })
    assert st["verdict"] == "pass"


async def test_hitl_small_auto_approve():
    """小额低风险自动通过（不 interrupt）"""
    st = await human_in_the_loop_node({
        "ai_conclusion": {"verdict": "pass"}, "ai_verdict": "pass",
        "total_amount": 500, "order_no": "PO-T",
    })
    assert st["final_verdict"] == "approved"
    assert st["teacher_decision"]["decision"] == "approved"
