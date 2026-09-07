# Revit 2020 / IronPython: export wall vertical constraints for diagnostics.
import io
import json
import os

from Autodesk.Revit.DB import (BuiltInParameter, FilteredElementCollector,
                               Wall)

M = 0.3048
OUT = os.environ.get("BIM_WALL_LEVEL_AUDIT_OUTPUT", os.path.join(
    os.environ.get("BIM_JSON_IN", ""), "wall_level_audit.json"))
doc = __revit__.ActiveUIDocument.Document


def value(param):
    return round(param.AsDouble() * M, 6) if param else None


rows = []
for wall in FilteredElementCollector(doc).OfClass(Wall):
    level = doc.GetElement(wall.LevelId)
    top_param = wall.get_Parameter(BuiltInParameter.WALL_HEIGHT_TYPE)
    top_id = top_param.AsElementId() if top_param else None
    top_level = doc.GetElement(top_id) if top_id else None
    bbox = wall.get_BoundingBox(None)
    rows.append({
        "id": wall.Id.IntegerValue,
        "base_level": level.Name if level else None,
        "base_level_elevation_m": round(level.Elevation * M, 6) if level else None,
        "base_offset_m": value(wall.get_Parameter(BuiltInParameter.WALL_BASE_OFFSET)),
        "top_level": top_level.Name if top_level else None,
        "top_offset_m": value(wall.get_Parameter(BuiltInParameter.WALL_TOP_OFFSET)),
        "unconnected_height_m": value(wall.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM)),
        "bbox_min_z_m": round(bbox.Min.Z * M, 6) if bbox else None,
        "bbox_max_z_m": round(bbox.Max.Z * M, 6) if bbox else None,
    })

with io.open(OUT, "w", encoding="utf-8") as stream:
    stream.write(json.dumps({"count": len(rows), "walls": rows}, indent=1))
