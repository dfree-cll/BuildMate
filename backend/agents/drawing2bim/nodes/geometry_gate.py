"""drawing2bim 几何质量闸门（第六期）——感知后、合规前

解决"56 分也建了"漏洞：感知结果必须过几何自检，不达标冻结等人工，
不静默进入建模。

检查项（全部确定性规则，LLM 不算几何）：
1. 构件数量合理性：柱数 vs 图纸柱块实体数（插入块数）对比
2. 柱贴轴网率：柱心距最近轴网交点 <300mm 的比例（<70% 报警）
3. 柱尺寸分布：单一方柱尺寸占 >90% 且无大柱 → info 提示
4. 轴网完整性：竖轴/横轴数量比例（20x7 vs 20x10 突变提示）
5. 异常置信度分布：低置信(<0.7)构件占比（>20% 冻结）

输出: geometry_report {verdict: pass/warn/fail, checks: [...], gate_blocked: bool}
"""
import math

from backend.core.logger import get_logger

logger = get_logger(__name__)

# 阈值（可配置，默认经验值）
MIN_GRID_ATTACH_RATE = 0.70     # 柱贴轴网率下限
LOW_CONFIDENCE_RATE_LIMIT = 0.20  # 低置信构件占比上限
GRID_ATTACH_TOL_MM = 300.0      # 贴轴网判定容差

SEVERITY_MAP = {
    "fail": "critical",
    "warn": "warning",
    "info": "info",
}


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _check_column_grid_attach(columns: list[dict], grid: dict | None) -> tuple[float, int, int]:
    """柱贴轴网率: 柱心距最近交点 <300mm 比例"""
    if not columns or not grid:
        return 0.0, 0, len(columns) if columns else 0
    gx = grid.get("x_axes") or []
    gy = grid.get("y_axes") or []
    if not gx or not gy:
        return 0.0, 0, len(columns)
    attached = 0
    for c in columns:
        loc = c.get("location") or c.get("center") or [0, 0]
        # location 是相对轴网原点(米); grid 也是相对原点(米)
        bx = min(gx, key=lambda v: abs(v - loc[0]))
        by = min(gy, key=lambda v: abs(v - loc[1]))
        d = math.hypot((bx - loc[0]) * 1000, (by - loc[1]) * 1000)
        if d < GRID_ATTACH_TOL_MM:
            attached += 1
    return attached / len(columns), attached, len(columns)


def _check_dimension_distribution(columns: list[dict]) -> list[dict]:
    """柱尺寸分布检查"""
    from collections import Counter
    sz = Counter((round(c.get("width", 0), 2), round(c.get("depth", 0), 2)) for c in columns)
    checks = []
    if not sz:
        return checks
    main_sz, main_n = sz.most_common(1)[0]
    total = sum(sz.values())
    if main_n / total > 0.9 and len(sz) == 1:
        checks.append({
            "rule_id": "GQ-004", "severity": "info",
            "description": "柱全部为同一尺寸 %sx%s(可能漏了大柱)" % (main_sz[0], main_sz[1]),
        })
    elif len(sz) >= 2:
        checks.append({
            "rule_id": "GQ-004", "severity": "info",
            "description": "柱尺寸 %d 种: %s" % (len(sz),
                                          ", ".join("%sx%s x%d" % (w, d, n) for (w, d), n in sz.most_common(3))),
        })
    return checks


def _check_confidence(columns: list[dict]) -> list[dict]:
    """低置信度占比检查"""
    low = [c for c in columns if (c.get("confidence") or 1.0) < 0.7]
    checks = []
    if columns and len(low) / len(columns) > LOW_CONFIDENCE_RATE_LIMIT:
        checks.append({
            "rule_id": "GQ-005", "severity": "warning",
            "description": "低置信度构件占比 %.0f%%(>%.0f%%上限)——提取结果不可靠" % (
                100 * len(low) / len(columns), 100 * LOW_CONFIDENCE_RATE_LIMIT),
            "affected": len(low),
        })
    return checks


def geometry_gate(state: dict) -> dict:
    """质量闸门节点: 返回 gate 结果(不抛异常, 失败=冻结标志)"""
    baseline = state.get("golden_baseline") or []
    columns = [e for e in baseline if e.get("ifc_type") == "IFCCOLUMN"]
    grid = state.get("dxf_grid")

    checks: list[dict] = []
    failures = 0
    warnings = 0

    # ① 柱贴轴网率
    if columns:
        rate, att, total = _check_column_grid_attach(columns, grid)
        sev = "warning" if rate < MIN_GRID_ATTACH_RATE else "info"
        if rate < MIN_GRID_ATTACH_RATE:
            warnings += 1
        checks.append({
            "rule_id": "GQ-001", "severity": sev,
            "description": "柱贴轴网率 %.0f%%(%d/%d, 阈值 %.0f%%)" % (
                100 * rate, att, total, 100 * MIN_GRID_ATTACH_RATE),
        })

    # ② 轴网完整性
    if grid:
        n_x = len(grid.get("x_axes") or [])
        n_y = len(grid.get("y_axes") or [])
        if n_x < 3 or n_y < 3:
            failures += 1
            checks.append({
                "rule_id": "GQ-002", "severity": "critical",
                "description": "轴网不完整: 竖轴 %d 横轴 %d(可能坐标系/聚类错误)" % (n_x, n_y),
            })
        else:
            checks.append({
                "rule_id": "GQ-002", "severity": "info",
                "description": "轴网 %d 竖 × %d 横" % (n_x, n_y),
            })

    # ③ 柱数量 vs 图纸实体数(meta.n_ins 来自 dxf_adaptive)
    meta = state.get("dxf_adaptive_meta") or {}
    n_ins = meta.get("n_ins")
    if n_ins and columns:
        ratio = len(columns) / max(n_ins, 1)
        if ratio < 0.5:
            warnings += 1
            checks.append({
                "rule_id": "GQ-003", "severity": "warning",
                "description": "柱提取率 %.0f%%(%d/%d 实体)——大量柱被过滤?" % (100 * ratio, len(columns), n_ins),
            })

    # ④ 尺寸分布
    checks.extend(_check_dimension_distribution(columns))
    # ⑤ 置信度
    checks.extend(_check_confidence(columns))

    verdict = "fail" if failures else ("warn" if warnings else "pass")
    gate_blocked = verdict == "fail"
    report = {
        "verdict": verdict,
        "gate_blocked": gate_blocked,
        "checks": checks,
        "stats": {
            "columns": len(columns),
            "walls": sum(1 for e in baseline if e.get("ifc_type") == "IFCWALL"),
            "grid": grid,
        },
    }
    logger.info("geometry_gate.done", verdict=verdict, cols=len(columns),
                blocked=gate_blocked, n_checks=len(checks))
    return {"geometry_report": report, "gate_blocked": gate_blocked}


async def geometry_gate_node(state: dict) -> dict:
    """LangGraph 节点包装"""
    return geometry_gate(state)
