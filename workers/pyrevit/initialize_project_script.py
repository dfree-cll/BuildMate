# -*- coding: utf-8 -*-
"""Initialize a blank Revit document as a BuildMate project working model.

The caller writes ``project_setup.json`` to the shared BIM input directory,
then invokes ``main`` through pyRevit Routes while the desired blank document
is active.  Existing RVT files are never overwritten.
"""
import json
import os
import time

from Autodesk.Revit.DB import SaveAsOptions
from Autodesk.Revit.UI import TaskDialog


def _exchange_dir(env_name, relative_dir):
    configured = os.environ.get(env_name, "")
    if configured:
        return configured
    project_root = os.environ.get("BUILDMATE_PROJECT_ROOT", "")
    if project_root:
        candidate = os.path.join(project_root, relative_dir)
        return candidate if os.path.isdir(candidate) else ""
    return ""


JSON_DIR = _exchange_dir("BIM_JSON_IN", os.path.join("data", "runtime", "revit", "json_in"))
RVT_OUT = _exchange_dir("REVIT_OUTPUT_DIR", os.path.join("data", "runtime", "revit", "rvt_out"))


def main():
    setup_path = os.path.join(JSON_DIR, "project_setup.json") if JSON_DIR else ""
    if not setup_path or not os.path.isfile(setup_path):
        TaskDialog.Show("BuildMate", "project_setup.json not found")
        return
    with open(setup_path, "r") as handle:
        setup = json.load(handle)
    project_id = str(setup.get("project_id") or "").strip()
    model_path = str(setup.get("model_path") or "").strip()
    if not project_id or not model_path:
        TaskDialog.Show("BuildMate", "project_id or model_path missing")
        return
    if os.path.exists(model_path):
        TaskDialog.Show("BuildMate", "working model already exists; refusing overwrite")
        return
    out_dir = os.path.dirname(model_path)
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    doc = __revit__.ActiveUIDocument.Document
    options = SaveAsOptions()
    options.OverwriteExistingFile = False
    doc.SaveAs(model_path, options)
    if not os.path.isfile(model_path):
        raise RuntimeError("SaveAs did not create working model")
    result = {"status": "done", "project_id": project_id,
              "model_path": model_path, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    result_path = os.path.join(RVT_OUT, "project_%s_setup_result.json" % project_id)
    with open(result_path, "w") as handle:
        json.dump(result, handle, ensure_ascii=True, indent=1)
    return result
