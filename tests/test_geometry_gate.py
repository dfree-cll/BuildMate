"""geometry_gate(第六期) 单元测试——质量闸门 + 低置信 HITL 冻结"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.agents.drawing2bim.nodes.geometry_gate import geometry_gate
from backend.agents.drawing2bim.nodes.hitl_review import aggregate_violations


def _cols(n, x=5.0, conf=0.92, w=0.8, d=0.8):
    return [{
        "element_id": "c%d" % i, "ifc_type": "IFCCOLUMN",
        "location": [x, 9.0 + i * 0.001, 0.0], "width": w, "depth": d,
        "confidence": conf, "source": "dxf_adaptive",
    } for i in range(n)]


GRID_OK = {"x_axes": [0, 9, 18, 27, 36, 45, 54, 63], "y_axes": [0, 9, 18, 27, 36]}


def test_gate_pass_when_healthy():
    """健康输入 → pass, 不冻结"""
    state = {
        "golden_baseline": _cols(50, x=9.0) + [{"ifc_type": "IFCWALL", "element_id": "w1"}],
        "dxf_grid": GRID_OK,
        "dxf_adaptive_meta": {"n_ins": 50},
    }
    r = geometry_gate(state)
    assert r["gate_blocked"] is False
    assert r["geometry_report"]["verdict"] == "pass"


def test_gate_block_on_broken_grid():
    """轴网 <3 轴 → fail 冻结"""
    state = {
        "golden_baseline": _cols(10),
        "dxf_grid": {"x_axes": [0, 9], "y_axes": [0]},
    }
    r = geometry_gate(state)
    assert r["gate_blocked"] is True
    assert r["geometry_report"]["verdict"] == "fail"
    sev = [c["severity"] for c in r["geometry_report"]["checks"]]
    assert "critical" in sev


def test_gate_warn_on_low_attach():
    """柱贴轴网率低(<70%) → warn(不冻结但列人工确认项)"""
    # 柱全部在 0.5m 处(远离 9m 网格)
    state = {
        "golden_baseline": _cols(30, x=0.5),
        "dxf_grid": GRID_OK,
    }
    r = geometry_gate(state)
    assert r["gate_blocked"] is False
    sev = [c["severity"] for c in r["geometry_report"]["checks"]]
    assert "warning" in sev


def test_gate_warn_on_extraction_ratio():
    """柱提取率 <50% → warn"""
    state = {
        "golden_baseline": _cols(20, x=9.0),
        "dxf_grid": GRID_OK,
        "dxf_adaptive_meta": {"n_ins": 100},
    }
    r = geometry_gate(state)
    sev = [c["severity"] for c in r["geometry_report"]["checks"]]
    assert "warning" in sev


def test_aggregate_violations_dedup():
    """同规则违规聚合(不刷屏)"""
    vs = [{"rule_id": "GQ-001", "severity": "warning", "track": "hard",
           "description": "贴轴网率低", "element_id": "c%d" % i} for i in range(20)]
    agg = aggregate_violations(vs)
    assert len(agg) == 1
    assert agg[0]["affected"] == 20
