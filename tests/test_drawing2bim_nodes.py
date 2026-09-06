"""drawing2bim 节点纯函数单元测试（第一期）

覆盖：硬轨规则引擎（HR-001~HR-004）、报告融合器（风险定级/verdict/低置信度）、
感知节点 IFC 结果转换。软轨 LLM 在 Mock 模式下返回空清单（确定性）。
"""
from backend.agents.drawing2bim.nodes.compliance import hard_track_review
from backend.agents.drawing2bim.nodes.report_merge import merge_reports
from backend.agents.drawing2bim.nodes.perception import _parsed_to_baseline


# ── 硬轨规则引擎 ────────────────────────────────────────────────────────────

def test_hard_rule_wall_thickness_missing():
    """HR-001：墙体无厚度属性 → critical"""
    baseline = [{"element_id": "w1", "ifc_type": "IFCWALL", "name": "W1",
                 "properties": {"Material": "砖"}}]
    vs = hard_track_review(baseline)
    ids = {v["rule_id"] for v in vs}
    assert "HR-001" in ids
    assert all(v["track"] == "hard" and v["confidence"] == 1.0 for v in vs)


def test_hard_rule_wall_thickness_zero():
    baseline = [{"element_id": "w1", "ifc_type": "IFCWALL", "name": "W1",
                 "properties": {"Thickness": 0, "Material": "砖"}}]
    assert any(v["rule_id"] == "HR-001" for v in hard_track_review(baseline))


def test_hard_rule_slab_elevation_missing():
    """HR-002：楼板无标高 → critical"""
    baseline = [{"element_id": "s1", "ifc_type": "IFCSLAB", "name": "B1",
                 "properties": {"Material": "C30"}}]
    assert any(v["rule_id"] == "HR-002" for v in hard_track_review(baseline))


def test_hard_rule_unnamed():
    """HR-003：未命名构件 → warning"""
    baseline = [{"element_id": "w1", "ifc_type": "IFCWALL", "name": "未命名",
                 "properties": {"Thickness": 240, "Material": "砖"}}]
    assert any(v["rule_id"] == "HR-003" for v in hard_track_review(baseline))


def test_hard_rule_material_missing():
    """HR-004：墙缺材料属性 → warning（仅 IFC 源启用——图纸源跳过防误报）"""
    baseline = [{"element_id": "w1", "ifc_type": "IFCWALL", "name": "W1",
                 "properties": {"Thickness": 240}}]
    assert any(v["rule_id"] == "HR-004" for v in hard_track_review(baseline, source="ifc"))
    # 图纸源（默认）跳过 IFC 语义规则：HR-004 不触发
    assert not any(v["rule_id"] == "HR-004" for v in hard_track_review(baseline))


def test_hard_rule_clean_element_passes():
    """合规构件：零违规"""
    baseline = [
        {"element_id": "w1", "ifc_type": "IFCWALL", "name": "外墙-W1",
         "properties": {"Thickness": 240, "Material": "烧结多孔砖"}},
        {"element_id": "s1", "ifc_type": "IFCSLAB", "name": "楼板-B1",
         "properties": {"Thickness": 120, "Elevation": 3.0, "Material": "C30"}},
    ]
    assert hard_track_review(baseline) == []


# ── 报告融合器 ──────────────────────────────────────────────────────────────

def test_merge_empty_passes():
    merged = merge_reports([], [])
    assert merged["verdict"] == "pass"
    assert merged["risk_level"] == "low"
    assert merged["hard_count"] == 0 and merged["soft_count"] == 0


def test_merge_critical_forces_high_risk():
    hard = [{"rule_id": "HR-001", "severity": "critical", "track": "hard",
             "element_id": "w1", "description": "墙体厚度缺失", "confidence": 1.0}]
    merged = merge_reports(hard, [])
    assert merged["risk_level"] == "high"
    assert merged["verdict"] == "review"
    assert merged["hard_critical_count"] == 1


def test_merge_warning_gives_medium_risk():
    hard = [{"rule_id": "HR-003", "severity": "warning", "track": "hard",
             "element_id": "w1", "description": "未命名", "confidence": 1.0}]
    merged = merge_reports(hard, [])
    assert merged["risk_level"] == "medium"
    assert merged["verdict"] == "review"


def test_merge_low_confidence_soft_triggers_review():
    """软轨低置信度条目 → verdict review 且计入待确认清单"""
    soft = [{"rule_id": "llm_soft", "severity": "info", "track": "soft",
             "element_id": "w1", "description": "疑似命名异常", "confidence": 0.3}]
    merged = merge_reports([], soft)
    assert merged["verdict"] == "review"
    assert merged["low_confidence_count"] == 1
    assert merged["low_confidence_items"][0]["rule_id"] == "llm_soft"


def test_merge_high_confidence_soft_passes():
    """软轨高置信度且无违规 → pass（不需要人工介入）"""
    soft = [{"rule_id": "llm_soft", "severity": "info", "track": "soft",
             "element_id": "w1", "description": "轻微建议", "confidence": 0.9}]
    merged = merge_reports([], soft)
    assert merged["verdict"] == "pass"
    assert merged["low_confidence_count"] == 0


def test_merge_hard_sorted_before_soft():
    """硬轨条目排在软轨之前（确定性输出优先）"""
    hard = [{"rule_id": "HR-001", "severity": "critical", "track": "hard",
             "element_id": "w1", "description": "h", "confidence": 1.0}]
    soft = [{"rule_id": "llm_soft", "severity": "critical", "track": "soft",
             "element_id": "w2", "description": "s", "confidence": 0.8}]
    merged = merge_reports(hard, soft)
    assert merged["violations"][0]["track"] == "hard"


# ── 感知节点：IFC 解析结果转换 ──────────────────────────────────────────────

def test_parsed_to_baseline_maps_types():
    parsed = {"elements": [
        {"type": "墙", "name": "W1", "global_id": "gid-1"},
        {"type": "门", "name": "D1", "global_id": "gid-2"},
    ]}
    baseline = _parsed_to_baseline(parsed)
    assert len(baseline) == 2
    assert baseline[0]["ifc_type"] == "IFCWALL"
    assert baseline[0]["element_id"] == "gid-1"
    assert baseline[1]["ifc_type"] == "IFCDOOR"
    assert all(e["review_status"] == "pending" for e in baseline)
