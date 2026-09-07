# Revit 2020 / IronPython: add verified GBZ/YBZ profile solids.
import datetime
import io
import json
import os
import traceback

from Autodesk.Revit.DB import (BuiltInCategory, BuiltInParameter, CurveLoop,
                               DirectShape, ElementId, FilteredElementCollector,
                               GeometryCreationUtilities, GeometryObject, Line,
                               Transaction, TransactionStatus, XYZ)
from Autodesk.Revit.UI import TaskDialog
from System.Collections.Generic import List

MM = 304.8
JSON_DIR = os.environ.get("BIM_JSON_IN", "")
INPUT = os.environ.get("BIM_BOUNDARY_COLUMNS_INPUT", os.path.join(
    JSON_DIR, "boundary_columns.json") if JSON_DIR else "")
OUT_DIR = os.environ.get("REVIT_OUTPUT_DIR", "")
MAX_LABEL_DISTANCE_MM = 2000.0
doc = __revit__.ActiveUIDocument.Document


def solid_for(item):
    cx, cy = float(item["x"]) / MM, float(item["y"]) / MM
    z0, z1 = float(item["base"]) / MM, float(item["top"]) / MM
    points = [XYZ(cx + float(p[0]) / MM, cy + float(p[1]) / MM, z0)
              for p in item["profile"]["points"]]
    loop = CurveLoop()
    for index, start in enumerate(points):
        end = points[(index + 1) % len(points)]
        if start.DistanceTo(end) > 1e-8:
            loop.Append(Line.CreateBound(start, end))
    return GeometryCreationUtilities.CreateExtrusionGeometry(
        [loop], XYZ.BasisZ, z1 - z0)


def main():
    if not INPUT or not os.path.isfile(INPUT):
        raise RuntimeError("请配置 BIM_BOUNDARY_COLUMNS_INPUT 或 BIM_JSON_IN")
    if not OUT_DIR:
        raise RuntimeError("请配置 REVIT_OUTPUT_DIR")
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    with io.open(INPUT, "r", encoding="utf-8") as stream:
        data = json.load(stream)
    candidates = [item for item in data.get("columns", [])
                  if float(item.get("label_distance_mm", 1e9)) <= MAX_LABEL_DISTANCE_MM]
    existing = {}
    for shape in FilteredElementCollector(doc).OfClass(DirectShape):
        try:
            if shape.ApplicationId == "BuildMate" and shape.ApplicationDataId:
                existing[shape.ApplicationDataId] = shape
        except Exception:
            pass
    records, failures = [], []
    tx = Transaction(doc, "BuildMate Add Boundary Columns")
    tx.Start()
    try:
        for item in candidates:
            name = "DS_col_%s_%s" % (item["type_name"], item["id"])
            if item["id"] in existing:
                shape = existing[item["id"]]
                shape.Name = name
                # Matching IDs are deliberately stable across re-extractions.
                # Updating only metadata here leaves a previously misplaced
                # DirectShape at its old coordinates.
                geometry = List[GeometryObject]()
                geometry.Add(solid_for(item))
                shape.SetShape(geometry)
                comment = shape.get_Parameter(
                    BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if comment and not comment.IsReadOnly:
                    comment.Set("BuildMate:auto|BoundaryColumn|%s|%s" % (
                        item["id"], item["type_name"]))
                records.append({"id": item["id"], "type_name": item["type_name"],
                                "revit_id": shape.Id.IntegerValue,
                                "status": "geometry_updated"})
                continue
            try:
                solid = solid_for(item)
                shape = DirectShape.CreateElement(
                    doc, ElementId(BuiltInCategory.OST_GenericModel))
                shape.ApplicationId = "BuildMate"
                shape.ApplicationDataId = str(item["id"])
                shape.Name = name
                geometry = List[GeometryObject]()
                geometry.Add(solid)
                shape.SetShape(geometry)
                comment = shape.get_Parameter(
                    BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if comment and not comment.IsReadOnly:
                    comment.Set("BuildMate:auto|BoundaryColumn|%s|%s" % (
                        item["id"], item["type_name"]))
                records.append({"id": item["id"], "type_name": item["type_name"],
                                "revit_id": shape.Id.IntegerValue,
                                "status": "created"})
            except Exception as ex:
                failures.append({"id": item.get("id"), "error": str(ex)[:200]})
        tx.Commit()
    except Exception:
        if tx.GetStatus() == TransactionStatus.Started:
            tx.RollBack()
        raise

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    model_path = os.path.join(OUT_DIR, "json_model_boundary_patch_%s.rvt" % stamp)
    result_path = os.path.join(OUT_DIR, "boundary_patch_%s_result.json" % stamp)
    doc.SaveAs(model_path)
    result = {"status": "done" if not failures else "partial",
              "requested": len(candidates), "updated": len(records),
              "failures": failures, "mapping": records,
              "deferred": len(data.get("columns", [])) - len(candidates)
                          + len(data.get("unmatched_labels", [])),
              "rvt_path": model_path}
    with io.open(result_path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=1))
    TaskDialog.Show("BuildMate Boundary Columns",
                    "created/existing %d / %d | failed %d | deferred %d\n%s" % (
                        len(records), len(candidates), len(failures),
                        result["deferred"], model_path))
