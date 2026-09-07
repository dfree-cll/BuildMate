"""BIM 模型确定性合规规则。

基于 ifc_parser 提取的结构化数据做完整性/质量检查；LLM 审查已统一迁移到
v2 workflow/Agent 链路，避免这里保留第二套审查合同。
  - 模型为空 / 无构件 → high
  - 无空间（IFCSPACE）→ medium（无法做房间级合规核查）
  - 无属性集（IFCPROPERTYSET）→ medium（缺材料/尺寸参数，后续规则无从校验）
  - 未命名构件占比 > 50% → low（建模规范问题，影响构件追溯）
  - schema 为 IFC2X3 → low（建议 IFC4，交付/协同兼容性更好）
"""


def run_rule_checks(parsed: dict) -> list[dict]:
    """规则引擎：对解析结果做确定性检查，返回 [{priority, description}]"""
    issues: list[dict] = []
    total = parsed.get("total_elements", 0)
    elements = parsed.get("elements", [])
    if total == 0:
        issues.append({"priority": "high", "description": "模型中未提取到任何建筑构件（墙/柱/梁/板/门窗等），模型可能为空或类型缺失"})
    if not parsed.get("total_spaces"):
        issues.append({"priority": "medium", "description": "模型未定义空间（IFCSPACE），无法进行房间级功能/面积合规核查"})
    if not parsed.get("properties"):
        issues.append({"priority": "medium", "description": "模型无属性集（IFCPROPERTYSET），缺少材料/尺寸等参数信息，深度合规校验无据可依"})
    unnamed = sum(1 for e in elements if not e.get("name") or e["name"] == "未命名")
    if elements and unnamed / len(elements) > 0.5:
        issues.append({"priority": "low",
                       "description": f"未命名构件占比过高（{unnamed}/{len(elements)}），影响构件追溯与审查"})
    if str(parsed.get("schema", "")).upper().startswith("IFC2X3"):
        issues.append({"priority": "low", "description": "模型使用 IFC2X3 schema，建议升级 IFC4 以获得更好的交付与协同兼容性"})
    return issues
