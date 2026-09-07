# Revit 2020 / IronPython: align wall geometry Z with its base level.
import datetime
import io
import json
import os
import traceback

from Autodesk.Revit.DB import (BuiltInParameter, FilteredElementCollector,
                               Line, Transaction, TransactionStatus, Wall, XYZ)
from Autodesk.Revit.UI import TaskDialog

M = 0.3048
OUT_DIR = os.environ.get("REVIT_OUTPUT_DIR", "")
doc = __revit__.ActiveUIDocument.Document


def main():
    if not OUT_DIR:
        raise RuntimeError("请配置 REVIT_OUTPUT_DIR")
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    records = []
    failures = []
    tx = Transaction(doc, "BuildMate Align Wall Levels")
    tx.Start()
    try:
        for wall in FilteredElementCollector(doc).OfClass(Wall):
            try:
                level = doc.GetElement(wall.LevelId)
                curve = wall.Location.Curve
                if level is None or curve is None:
                    raise ValueError("wall level or curve missing")
                start = curve.GetEndPoint(0)
                end = curve.GetEndPoint(1)
                z = level.Elevation
                wall.Location.Curve = Line.CreateBound(
                    XYZ(start.X, start.Y, z), XYZ(end.X, end.Y, z))
                base_offset = wall.get_Parameter(BuiltInParameter.WALL_BASE_OFFSET)
                if base_offset and not base_offset.IsReadOnly:
                    base_offset.Set(0.0)
                records.append({"id": wall.Id.IntegerValue,
                                "base_level": level.Name,
                                "z_m": round(z * M, 6)})
            except Exception as ex:
                failures.append({"id": wall.Id.IntegerValue,
                                 "error": str(ex)[:200]})
        tx.Commit()
    except Exception:
        if tx.GetStatus() == TransactionStatus.Started:
            tx.RollBack()
        raise

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    model_path = os.path.join(OUT_DIR, "json_model_wall_level_patch_%s.rvt" % stamp)
    result_path = os.path.join(OUT_DIR, "wall_level_patch_%s_result.json" % stamp)
    doc.SaveAs(model_path)
    result = {"status": "done" if not failures else "partial",
              "requested": len(records) + len(failures),
              "updated": len(records), "failures": failures,
              "rvt_path": model_path}
    with io.open(result_path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=1))
    TaskDialog.Show("BuildMate Wall Level Repair",
                    "updated %d / %d | failed %d\n%s" % (
                        len(records), len(records) + len(failures),
                        len(failures), model_path))


try:
    main()
except Exception:
    TaskDialog.Show("BuildMate Wall Level Repair Error",
                    traceback.format_exc()[:1600])
