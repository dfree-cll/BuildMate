# -*- coding: utf-8 -*-
from Autodesk.Revit.DB import (BooleanOperationsType, BooleanOperationsUtils,
                               BoundingBoxIntersectsFilter, BuiltInParameter,
                               Element, ElementIntersectsElementFilter,
                               FamilyInstance, FilteredElementCollector,
                               GeometryInstance, JoinGeometryUtils, Options,
                               Outline, Solid, Wall)

MM = 304.8
doc = __revit__.ActiveUIDocument.Document


def _family_name(instance):
    try:
        return Element.Name.GetValue(instance.Symbol.Family)
    except Exception:
        return ""


def _bbox(element):
    box = element.get_BoundingBox(None)
    if box is None:
        return None
    return {
        "min": [round(box.Min.X * MM, 3), round(box.Min.Y * MM, 3),
                round(box.Min.Z * MM, 3)],
        "max": [round(box.Max.X * MM, 3), round(box.Max.Y * MM, 3),
                round(box.Max.Z * MM, 3)],
    }


def _solids(element):
    result = []
    def collect(geometry):
        for item in geometry or []:
            if isinstance(item, Solid) and item.Volume > 1e-10:
                result.append(item)
            elif isinstance(item, GeometryInstance):
                collect(item.GetInstanceGeometry())
    collect(element.get_Geometry(Options()))
    return result


def _intersection_volume(first, second):
    total = 0.0
    for a in _solids(first):
        for b in _solids(second):
            try:
                total += BooleanOperationsUtils.ExecuteBooleanOperation(
                    a, b, BooleanOperationsType.Intersect).Volume
            except Exception:
                pass
    return total


def main():
    columns = [item for item in FilteredElementCollector(doc).OfClass(FamilyInstance)
               if _family_name(item).startswith("BM_Boundary_")]
    walls = list(FilteredElementCollector(doc).OfClass(Wall))
    current = []
    z_counts = {}
    for item in columns:
        parameter = item.get_Parameter(
            BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        marker = parameter.AsString() if parameter is not None else ""
        if "B=RVT-HOTEL-B1-STRUCT-012;" in (marker or ""):
            current.append(item)
        box = item.get_BoundingBox(None)
        key = str(round(box.Min.Z * MM)) if box is not None else "none"
        z_counts[key] = z_counts.get(key, 0) + 1
    first = current[0] if current else (columns[0] if columns else None)
    bbox_pairs = 0
    solid_pairs = 0
    joined_wall_pairs = 0
    cutting_wall_pairs = 0
    intersect_filter_pairs = 0
    for column in current:
        box = column.get_BoundingBox(None)
        candidates = list(FilteredElementCollector(doc).OfClass(Wall).WherePasses(
            BoundingBoxIntersectsFilter(Outline(box.Min, box.Max)))) if box else []
        bbox_pairs += len(candidates)
        intersect_filter_pairs += len(list(
            FilteredElementCollector(doc).OfClass(Wall).WherePasses(
                ElementIntersectsElementFilter(column))))
        solid_pairs += sum(1 for wall in candidates
                           if _intersection_volume(column, wall) > 1e-7)
        for joined_id in JoinGeometryUtils.GetJoinedElements(doc, column):
            joined = doc.GetElement(joined_id)
            if isinstance(joined, Wall):
                joined_wall_pairs += 1
                if JoinGeometryUtils.IsCuttingElementInJoin(
                        doc, column, joined):
                    cutting_wall_pairs += 1
    base_level = (first.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_PARAM)
        if first else None)
    base_offset = (first.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM)
        if first else None)
    return {"document": doc.Title, "is_family": doc.IsFamilyDocument,
            "column_count": len(columns), "current_count": len(current),
            "wall_count": len(walls), "z_min_counts": z_counts,
            "bbox_pairs": bbox_pairs, "solid_pairs": solid_pairs,
            "intersect_filter_pairs": intersect_filter_pairs,
            "joined_wall_pairs": joined_wall_pairs,
            "cutting_wall_pairs": cutting_wall_pairs}
