# -*- coding: utf-8 -*-
"""Audit final wall solid edges, not only Location.Curve."""
import json
import math
import os

from Autodesk.Revit.DB import (
    BuiltInParameter, FilteredElementCollector, Line, Options, Wall,
)

MM = 304.8
MARKER = "BUILDMATE_AUTO:"


def _input_id(wall):
    try:
        param = wall.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        value = param.AsString() if param is not None else ""
        for part in value.split(";"):
            if part.startswith("I="):
                return part[2:]
    except Exception:
        pass
    return "wall_%s" % wall.Id.IntegerValue


def main():
    doc = __revit__.ActiveUIDocument.Document
    out_dir = os.environ.get("REVIT_OUTPUT_DIR", "")
    if not out_dir:
        raise RuntimeError("请配置 REVIT_OUTPUT_DIR")
    rows = []
    options = Options()
    options.IncludeNonVisibleObjects = False
    for wall in FilteredElementCollector(doc).OfClass(Wall):
        try:
            param = wall.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            comments = param.AsString() if param is not None else ""
            if not comments.startswith(MARKER):
                continue
            curve = wall.Location.Curve
            length_mm = curve.Length * MM
            location_start = curve.GetEndPoint(0)
            location_end = curve.GetEndPoint(1)
            bad_edges = []
            geometry = wall.get_Geometry(options)
            for item in geometry:
                if not hasattr(item, "Edges"):
                    continue
                for edge in item.Edges:
                    edge_curve = edge.AsCurve()
                    if not isinstance(edge_curve, Line):
                        continue
                    p0 = edge_curve.GetEndPoint(0)
                    p1 = edge_curve.GetEndPoint(1)
                    dx = (p1.X - p0.X) * MM
                    dy = (p1.Y - p0.Y) * MM
                    planar_length = math.hypot(dx, dy)
                    if planar_length < 100.0:
                        continue
                    angle = abs(math.degrees(math.atan2(dy, dx))) % 90.0
                    deviation = min(angle, 90.0 - angle)
                    if deviation > 0.05:
                        bad_edges.append({
                            "length_mm": round(planar_length, 2),
                            "deviation_deg": round(deviation, 5),
                            "start": [round(p0.X * MM, 2), round(p0.Y * MM, 2)],
                            "end": [round(p1.X * MM, 2), round(p1.Y * MM, 2)],
                        })
            rows.append({"id": _input_id(wall),
                         "revit_id": wall.Id.IntegerValue,
                         "length_mm": round(length_mm, 2),
                         "location_start": [round(location_start.X * MM, 2),
                                            round(location_start.Y * MM, 2)],
                         "location_end": [round(location_end.X * MM, 2),
                                          round(location_end.Y * MM, 2)],
                         "skew_edge_count": len(bad_edges),
                         "skew_edges": bad_edges[:20]})
        except Exception as ex:
            rows.append({"revit_id": wall.Id.IntegerValue,
                         "error": str(ex)[:160]})
    result = {
        "wall_count": len(rows),
        "skewed_wall_count": sum(1 for row in rows
                                 if row.get("skew_edge_count", 0) > 0),
        "walls": rows,
    }
    path = os.path.join(out_dir, "wall_solid_audit.json")
    with open(path, "w") as stream:
        json.dump(result, stream, indent=1)
    print(json.dumps({"status": "done", "path": path,
                      "wall_count": result["wall_count"],
                      "skewed_wall_count": result["skewed_wall_count"]}))
    return result


main()
