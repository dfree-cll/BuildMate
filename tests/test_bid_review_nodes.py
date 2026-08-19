"""投标审查 Agent 单元测试（规则/并行/格式化）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.agents.bid_review.nodes import parse_node, format_node


async def test_parse_uses_mock_doc_when_empty():
    """空输入时使用 mock 投标文档"""
    st = await parse_node({"doc_text": "", "original_query": ""})
    assert len(st["doc_text"]) > 50, "应有 mock 文档内容"


async def test_format_generates_report():
    """格式化节点生成完整报告"""
    st = await format_node({
        "structured": {"project_name": "测试项目", "bidder": "测试公司"},
        "weighted_score": 80.5,
        "dimension_scores": [
            {"dimension": "技术方案", "weight": 0.35, "score": 85, "issues": ["问题1"], "suggestions": ["建议1"]},
        ],
        "issues": [{"priority": "high", "dimension": "技术", "description": "风险"}],
        "summary": {"overall_comment": "总体可行", "risk_level": "medium", "recommendation": "谨慎投标"},
    })
    assert "加权综合得分" in st["answer"]
    assert "80.5" in st["answer"]
