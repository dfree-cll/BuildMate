"""ModelIR：中间表示（model.json 数据结构 + 完整性校验）

AIM（AI Intermediate Model）——任何输入格式统一翻译成此结构：
  project(Project Context) / workflow_steps / grid / model_elements / views
"""
from __future__ import annotations

from typing import Any

REQUIRED_TOP = ("project", "grid", "model_elements")
ELEMENT_TYPES = ("Wall", "Column", "Beam", "Floor", "Slab")


def validate_ir(data: dict) -> list[str]:
    """校验中间表示完整性——返回问题列表（空=通过）"""
    issues: list[str] = []
    for key in REQUIRED_TOP:
        if key not in data:
            issues.append(f"缺少顶层字段: {key}")
    proj = data.get("project") or {}
    if "units" not in proj:
        issues.append("project 缺少 units")
    if "levels" not in proj:
        issues.append("project 缺少 levels")
    grid = data.get("grid") or {}
    if not grid.get("x_axes") and not grid.get("y_axes"):
        issues.append("grid 为空（无轴网）")
    elems = data.get("model_elements") or []
    if not elems:
        issues.append("model_elements 为空（无构件）")
    for i, e in enumerate(elems):
        if e.get("type") not in ELEMENT_TYPES:
            issues.append(f"model_elements[{i}] 未知类型: {e.get('type')}")
        if e.get("type") == "Wall":
            if not e.get("start") or not e.get("end"):
                issues.append(f"model_elements[{i}] 墙缺 start/end")
        elif e.get("type") == "Column":
            if e.get("x") is None or e.get("y") is None:
                issues.append(f"model_elements[{i}] 柱缺 x/y")
    return issues


class ModelIR:
    """中间表示对象——构建/校验/序列化"""

    def __init__(self, data: dict):
        self.data = data

    @property
    def issues(self) -> list[str]:
        return validate_ir(self.data)

    @property
    def valid(self) -> bool:
        return not self.issues

    def summary(self) -> dict:
        proj = self.data.get("project") or {}
        elems = self.data.get("model_elements") or []
        grid = self.data.get("grid") or {}
        return {
            "project": proj.get("name", ""),
            "units": proj.get("units", ""),
            "levels": len(proj.get("levels", [])),
            "grid": "%dx%d" % (len(grid.get("x_axes", [])), len(grid.get("y_axes", []))),
            "elements": {
                t: sum(1 for e in elems if e.get("type") == t)
                for t in ELEMENT_TYPES if any(e.get("type") == t for e in elems)
            },
            "valid": self.valid,
            "issues": self.issues[:5],
        }

    def to_json(self) -> dict:
        return self.data
