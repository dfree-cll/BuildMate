"""IFC 差异比对器 + 修正回退逻辑（架构图 T/U 节点：第一轮差异比对 + 回退修正）

比对基准：黄金基准 JSON（事实源）vs IFC 解析回读结果（parse_ifc_model 输出）
比对维度：
- 存在性（GlobalId）→ missing / extra
- 类型 / 名称
- 属性级（基准 properties vs IFC 回读 Psets/Quantities/Materials 拍平）
- 位置（IFC 回读 Container：构件所在楼层/空间，并入 detail 供自然语言报告）

修正回退（U 节点）：仅处理确定性、规则可判定的差异——
- 不支持的 ifc_type 导致构件缺失 → 从生成集剔除并记录（下轮不再产生该差异）
- 其余差异（生成器丢失数据）→ 黄金基准不可被修改，保留差异上报人工

收敛策略（对齐架构评审结论）：ε 容差 + 分类 + 最大迭代次数，
不追求"差异归零"死循环；超限后以 diffs_found 状态上报。
"""
from backend.core.logger import get_logger

logger = get_logger(__name__)

# 与生成器保持一致的可生成类型集
_GENERATABLE_TYPES = {
    "IFCWALL", "IFCCOLUMN", "IFCBEAM", "IFCSLAB",
    "IFCDOOR", "IFCWINDOW", "IFCSTAIR", "IFCROOF",
}

MAX_CORRECTION_ITERATIONS = 2

# 自然语言差异报告（LLM 生成：位置 + 构件 + 差异）
_NATURAL_DIFF_PROMPT = """你是 BIM 差异分析工程师。基于以下「图纸基准 vs 生成 IFC」的差异清单，输出一份**自然语言差异报告**（给工程人员看），要求：
1. 按差异类型分组描述，每条说明：**位置**（楼层/空间）、**哪个构件**（名称/类型）、**差异是什么**（缺失/属性不一致/类型错误等，含基准值与 IFC 值）
2. 语言通俗（大白话），工程人员能直接看懂
3. 若差异为空输出"图纸与生成 IFC 完全一致，无差异。"
4. 输出纯文本（不要 JSON、不要列表符号过重，可用段落式）

差异清单（JSON）：
{diffs_json}

生成 IFC 路径：{ifc_path}
"""


async def format_diff_report_natural(diffs: list[dict], ifc_path: str = "") -> str:
    """LLM 生成自然语言差异报告（失败降级为模板拼接）"""
    try:
        from backend.core.llm_factory import get_llm
        from langchain_core.messages import HumanMessage
        llm = get_llm("qa", temperature=0.2)
        resp = await llm.ainvoke([HumanMessage(content=_NATURAL_DIFF_PROMPT.format(
            diffs_json=__import__("json").dumps(diffs, ensure_ascii=False)[:4000],
            ifc_path=ifc_path))])
        text = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        if text:
            return text
    except Exception as e:
        logger.warning("ifc_diff.natural_report_failed", error=str(e)[:100])
    # 降级：模板拼接（含位置/构件/差异）
    if not diffs:
        return "图纸与生成 IFC 完全一致，无差异。"
    lines = ["图纸与生成 IFC 存在差异，明细如下："]
    for d in diffs:
        loc = f"（位置：{d.get('location')}）" if d.get("location") else ""
        lines.append(f"- {d.get('detail', '')}{loc}")
    return "\n".join(lines)


def _flatten_ifc_props(rt_elem: dict) -> dict:
    """IFC 回读构件 → 扁平属性 dict（Psets/Quantities/Materials 拍平，键名归一）
    Psets/Quantities 实际为 dict {集名: {属性: 值}}（get_psets 输出）；兼容 list 形态（测试构造）"""
    flat: dict = {}

    def _merge(container):
        """{集名: {属性: 值}} 或 [{集名: {属性: 值}}] 拍平"""
        if isinstance(container, dict):
            for v in container.values():
                if isinstance(v, dict):
                    flat.update({kk: vv for kk, vv in v.items() if not isinstance(vv, (dict, list))})
        elif isinstance(container, list):
            for pset in container:
                if isinstance(pset, dict):
                    for v in pset.values():
                        if isinstance(v, dict):
                            flat.update({kk: vv for kk, vv in v.items() if not isinstance(vv, (dict, list))})
                        elif not isinstance(v, (dict, list)):
                            flat[list(pset.keys())[0] if pset else ""] = v

    _merge(rt_elem.get("Psets"))
    _merge(rt_elem.get("Quantities"))
    # Materials：list of dict（{Name/Description}）或 list[str] 或 dict
    mats = []
    mraw = rt_elem.get("Materials")
    if isinstance(mraw, dict):
        mats.append(str(mraw.get("Name") or mraw.get("name") or ""))
    elif isinstance(mraw, list):
        for m in mraw:
            if isinstance(m, dict):
                mats.append(str(m.get("Name") or m.get("name") or m.get("Material") or ""))
            else:
                mats.append(str(m))
    if mats:
        flat["Material"] = "、".join(m for m in mats if m)
    return flat


def _norm_value(v) -> float | str:
    """数值/字符串归一（240 vs "240" 视为相等）"""
    if isinstance(v, str):
        v = v.strip()
        try:
            return float(v)
        except (ValueError, TypeError):
            return v
    return v


def compute_ifc_diffs(baseline: list[dict], parsed: dict) -> list[dict]:
    """纯函数：黄金基准 vs IFC 回读结果 → 差异清单（存在性/类型/名称/属性/位置）

    parsed: parse_ifc_model 的原始输出（含 elements 列表）
    每项差异：{element_id, kind, category(geometric/semantic), location, detail}
    """
    diffs: list[dict] = []
    rt_elements = parsed.get("elements", [])
    rt_by_id = {e.get("global_id") or e.get("GlobalId"): e for e in rt_elements}
    baseline_ids = set()

    for elem in baseline:
        eid = elem.get("element_id", "")
        baseline_ids.add(eid)
        rt = rt_by_id.get(eid)

        if rt is None:
            diffs.append({
                "element_id": eid, "kind": "missing", "category": "geometric",
                "location": elem.get("location") or elem.get("floor") or "",
                "detail": f"构件 {elem.get('name', '')} 在 IFC 中缺失",
            })
            continue

        base_type = (elem.get("ifc_type") or "").upper()
        rt_type = (rt.get("ifc_type") or "").upper()
        if base_type != rt_type:
            diffs.append({
                "element_id": eid, "kind": "type_mismatch", "category": "semantic",
                "location": elem.get("location") or elem.get("floor") or "",
                "detail": f"类型不一致：基准 {base_type} vs IFC {rt_type}",
            })
        if (rt.get("name") or "") != (elem.get("name") or ""):
            diffs.append({
                "element_id": eid, "kind": "name_mismatch", "category": "semantic",
                "location": elem.get("location") or elem.get("floor") or "",
                "detail": f"名称不一致：基准「{elem.get('name', '')}」vs IFC「{rt.get('name', '')}」",
            })

        # ── 属性级对比：基准 properties vs IFC Psets/Quantities/Materials ──
        base_props = elem.get("properties") or {}
        if base_props:
            rt_flat = _flatten_ifc_props(rt)
            for pk, pv in base_props.items():
                rv = rt_flat.get(pk)
                if rv is None:
                    diffs.append({
                        "element_id": eid, "kind": "property_missing",
                        "category": "semantic",
                        "location": elem.get("location") or elem.get("floor") or "",
                        "detail": f"属性缺失：{elem.get('name', '')} 的「{pk}」（基准 {pv}）在 IFC 中不存在",
                    })
                elif _norm_value(rv) != _norm_value(pv):
                    diffs.append({
                        "element_id": eid, "kind": "property_mismatch",
                        "category": "semantic",
                        "location": elem.get("location") or elem.get("floor") or "",
                        "detail": f"属性不一致：{elem.get('name', '')} 的「{pk}」基准 {pv} vs IFC {rv}",
                    })

        # ── 位置比对：IFC 回读 Container（楼层/空间）并入 detail ──
        container = rt.get("Container")
        if container and elem.get("location"):
            cname = str(container)
            if cname and cname != str(elem.get("location")):
                diffs.append({
                    "element_id": eid, "kind": "location_mismatch",
                    "category": "semantic",
                    "location": f"{elem.get('location')}（IFC 回读：{cname}）",
                    "detail": f"位置不一致：{elem.get('name', '')} 基准位于 {elem.get('location')}，IFC 中位于 {cname}",
                })

    # IFC 中多出的构件（不在基准内）
    for eid in rt_by_id:
        if eid and eid not in baseline_ids:
            diffs.append({
                "element_id": eid, "kind": "extra", "category": "semantic",
                "location": str(rt_by_id[eid].get("Container") or ""),
                "detail": "IFC 中存在基准之外的构件",
            })
    return diffs


def apply_corrections(baseline: list[dict], diffs: list[dict]) -> tuple[list[dict], list[str]]:
    """纯函数：修正回退（IFC修正回退Agent 的规则部分）

    仅对确定性可修正的差异动手；黄金基准本身不被篡改——
    修正体现在"本轮生成集"的调整上，并返回修正说明。
    """
    missing_ids = {d["element_id"] for d in diffs if d["kind"] == "missing"}
    corrected: list[dict] = []
    applied: list[str] = []
    excluded: list[str] = []

    for elem in baseline:
        eid = elem.get("element_id", "")
        ifc_type = (elem.get("ifc_type") or "").upper()
        if eid in missing_ids and ifc_type not in _GENERATABLE_TYPES:
            # 生成器不支持的类型 → 本轮剔除，避免无限重试
            excluded.append(eid)
            applied.append(f"剔除不可生成构件 {eid}（{ifc_type}）")
            continue
        corrected.append(elem)

    if applied:
        logger.info("ifc_diff.corrections_applied", count=len(applied),
                    excluded=len(excluded))
    return corrected, applied
