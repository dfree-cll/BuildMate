# Revit 2020 / IronPython: export BuildMate boundary-column bounding boxes.
import io
import json
import os

from Autodesk.Revit.DB import DirectShape, FilteredElementCollector

M = 0.3048
OUT = os.environ.get("BIM_BOUNDARY_AUDIT_OUTPUT", os.path.join(
    os.environ.get("BIM_JSON_IN", ""), "boundary_revit_audit.json"))
doc = __revit__.ActiveUIDocument.Document

rows = []
for shape in FilteredElementCollector(doc).OfClass(DirectShape):
    try:
        key = shape.ApplicationDataId or ""
        name = shape.Name or ""
        box = shape.get_BoundingBox(None)
        if box is None:
            continue
        rows.append({
            "id": key or name.rsplit("_", 1)[-1], "name": name,
            "application_id": shape.ApplicationId, "revit_id": shape.Id.IntegerValue,
            "min": [round(box.Min.X * M, 3), round(box.Min.Y * M, 3),
                    round(box.Min.Z * M, 3)],
            "max": [round(box.Max.X * M, 3), round(box.Max.Y * M, 3),
                    round(box.Max.Z * M, 3)],
        })
    except Exception:
        pass
with io.open(OUT, "w", encoding="utf-8") as stream:
    stream.write(json.dumps({"count": len(rows), "items": rows}, indent=1))
