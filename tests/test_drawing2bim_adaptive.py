"""dxf_adaptive(第六期) 单元测试——真实 DXF 回归 + 特征自适应

覆盖:
1. SG20 114 柱 / 20x7 轴网 / rot 6.37 / 轴距 9000(与手写版一致)
2. B1 204 柱 / 20x10 轴网 / 标高 -6.5~-0.1
3. 钢结构图正确拒绝(无柱块)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.agents.drawing2bim.nodes.dxf_adaptive import (
    extract_columns_from_dxf, find_col_block, _auto_pitch, _rect_center_edges,
)

SG20 = os.environ.get("BUILDMATE_TEST_STRUCTURAL_DXF", "")
B1 = os.environ.get("BUILDMATE_TEST_BASEMENT_DXF", "")


@pytest.mark.skipif(not SG20 or not os.path.exists(SG20),
                    reason="未配置外部结构 DXF 回归样本")
def test_sg20_columns_grid_rot():
    r = extract_columns_from_dxf(SG20)
    assert len(r["columns"]) == 114
    assert len(r["grid"]["x_axes"]) == 20
    assert len(r["grid"]["y_axes"]) == 7
    assert abs(r["meta"]["rot_deg"] - 6.37) < 0.1
    assert [round(float(v)) for v in r["meta"]["pitch_mm"]] == [9000, 9000]
    # 柱元素字段完整
    c = r["columns"][0]
    assert c["ifc_type"] == "IFCCOLUMN"
    assert c["width"] == 0.8 and c["depth"] == 0.8
    assert c["confidence"] == 0.92
    assert "grid_ref" in c


@pytest.mark.skipif(not B1 or not os.path.exists(B1),
                    reason="未配置外部地下层 DXF 回归样本")
def test_b1_columns_levels():
    r = extract_columns_from_dxf(B1)
    assert len(r["columns"]) == 204
    assert len(r["grid"]["x_axes"]) == 20
    assert len(r["grid"]["y_axes"]) == 10
    assert r["meta"]["elev_m"] == [-6.5, -0.1]  # 块名括号标高解析


def test_auto_pitch_detects_9m():
    vals = [0, 9000, 18000, 27000, 36000, 45000, 54000, 63000]
    assert _auto_pitch(vals) == 9000.0


def test_rect_center_edges():
    pts = [(-1018, 881), (-1018, 81), (-218, 81), (-218, 881)]
    rc = _rect_center_edges(pts)
    assert rc is not None
    cx, cy, edges, (ldx, ldy) = rc
    assert abs(cx - (-618)) < 1 and abs(cy - 481) < 1
    assert sorted(edges) == [800, 800, 800, 800]  # 800×800 方柱
    assert ldx > ldy  # 长边(等边时取第一条)沿 X


def test_rect_center_rejects_non_rect():
    assert _rect_center_edges([(0, 0), (100, 0), (100, 50), (50, 50)]) is None
    assert _rect_center_edges([(0, 0), (100, 0)]) is None
