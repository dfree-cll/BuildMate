# Revit 2020 / IronPython: remove only BuildMate temporary boundary profiles.
import datetime
import io
import json
import os

from Autodesk.Revit.DB import DirectShape, ElementId, FilteredElementCollector, Transaction
from System.Collections.Generic import List

OUT_DIR = os.environ.get("REVIT_OUTPUT_DIR", "")
doc = __revit__.ActiveUIDocument.Document


def main():
    if not OUT_DIR:
        raise RuntimeError("请配置 REVIT_OUTPUT_DIR")
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    ids = []
    for shape in FilteredElementCollector(doc).OfClass(DirectShape):
        try:
            key = shape.ApplicationDataId or ""
            if shape.ApplicationId == "BuildMate" and key.startswith("boundary_"):
                ids.append(shape.Id)
        except Exception:
            pass
    tx = Transaction(doc, "BuildMate Replace Boundary Columns")
    tx.Start()
    if ids:
        targets = List[ElementId]()
        for item in ids:
            targets.Add(item)
        doc.Delete(targets)
    tx.Commit()
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    model_path = os.path.join(OUT_DIR, "json_model_boundary_reset_%s.rvt" % stamp)
    result_path = os.path.join(OUT_DIR, "boundary_reset_%s_result.json" % stamp)
    doc.SaveAs(model_path)
    with io.open(result_path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps({"removed": len(ids), "rvt_path": model_path}, indent=1))
