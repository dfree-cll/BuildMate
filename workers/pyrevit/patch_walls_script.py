# Local wall repair for Revit 2020 / IronPython.
import datetime
import json
import os
import traceback

from Autodesk.Revit.DB import (ElementId, Line, Transaction,
                               TransactionStatus, Wall, WallUtils, XYZ)
from Autodesk.Revit.UI import TaskDialog

MM = 304.8
MANIFEST = os.environ.get("BIM_WALL_ITERATION_MANIFEST", os.path.join(
    os.environ.get("BIM_JSON_IN", ""), "wall_iteration.json"))
OUT_DIR = os.environ.get("REVIT_OUTPUT_DIR", "")
doc = __revit__.ActiveUIDocument.Document


def xyz(point):
    z = point[2] if len(point) > 2 else 0.0
    return XYZ(float(point[0]) / MM, float(point[1]) / MM, float(z) / MM)


def main():
    if not MANIFEST or not os.path.isfile(MANIFEST):
        raise RuntimeError("请配置 BIM_WALL_ITERATION_MANIFEST 或 BIM_JSON_IN")
    if not OUT_DIR:
        raise RuntimeError("请配置 REVIT_OUTPUT_DIR")
    with open(MANIFEST, "r") as stream:
        manifest = json.load(stream)
    repairs = [item for item in manifest.get("repairs", [])
               if item.get("action") == "update"]
    records = []
    failures = []
    staged = []

    create_tx = Transaction(doc, "BuildMate Disable Wall Joins")
    create_tx.Start()
    try:
        for repair in repairs:
            input_id = str(repair.get("element_id") or "")
            old_id = str(repair.get("revit_element_id") or "")
            try:
                old_wall = doc.GetElement(ElementId(int(old_id)))
                if old_wall is None or not isinstance(old_wall, Wall):
                    raise ValueError("source wall not found")
                fields = repair.get("fields") or {}
                start, end = fields["start"], fields["end"]
                WallUtils.DisallowWallJoinAtEnd(old_wall, 0)
                WallUtils.DisallowWallJoinAtEnd(old_wall, 1)
                staged.append((old_wall.Id, input_id, old_id, start, end))
            except Exception as ex:
                failures.append({"element_id": input_id, "stage": "create",
                                 "error": str(ex)[:200]})
        create_tx.Commit()
    except Exception:
        if create_tx.GetStatus() == TransactionStatus.Started:
            create_tx.RollBack()
        raise

    place_tx = Transaction(doc, "BuildMate Place Wall Repairs")
    place_tx.Start()
    try:
        for wall_id, input_id, old_id, start, end in staged:
            try:
                wall = doc.GetElement(wall_id)
                WallUtils.DisallowWallJoinAtEnd(wall, 0)
                WallUtils.DisallowWallJoinAtEnd(wall, 1)
                wall.Location.Curve = Line.CreateBound(xyz(start), xyz(end))
                records.append({"input_id": input_id, "old_revit_id": old_id,
                                "new_revit_id": wall_id.IntegerValue})
            except Exception as ex:
                failures.append({"element_id": input_id, "stage": "place",
                                 "error": str(ex)[:200]})
        place_tx.Commit()
    except Exception:
        if place_tx.GetStatus() == TransactionStatus.Started:
            place_tx.RollBack()
        raise

    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    result_path = os.path.join(OUT_DIR, "wall_patch_%s_result.json" % stamp)
    model_path = os.path.join(OUT_DIR, "json_model_wall_patch_%s.rvt" % stamp)
    doc.SaveAs(model_path)
    result = {"status": "done" if not failures else "partial",
              "requested": len(repairs), "updated": len(records),
              "failures": failures, "mapping": records,
              "rvt_path": model_path}
    with open(result_path, "w") as stream:
        json.dump(result, stream, indent=1)
    TaskDialog.Show("BuildMate Wall Repair",
                    "updated %d / %d | failed %d\n%s" % (
                        len(records), len(repairs), len(failures), model_path))


try:
    main()
except Exception:
    TaskDialog.Show("BuildMate Wall Repair Error", traceback.format_exc()[:1600])
