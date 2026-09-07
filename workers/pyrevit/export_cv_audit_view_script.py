# -*- coding: utf-8 -*-
"""Export the active Revit view at high resolution for independent CV QA."""
import json
import os
import time

from Autodesk.Revit.DB import (
    ExportRange, FilteredElementCollector, ImageExportOptions, ImageFileType, ImageResolution,
    View, Wall,
    ViewType, ZoomFitType,
)
from System.Collections.Generic import List
from Autodesk.Revit.DB import ElementId


def main():
    uidoc = __revit__.ActiveUIDocument
    doc = uidoc.Document
    view = None
    best_wall_count = -1
    # Build execution may leave a 3D view active. CV axis auditing must use the
    # floor plan that actually displays the most generated walls, never an
    # isometric projection or an empty template-controlled plan.
    for candidate in FilteredElementCollector(doc).OfClass(View):
        try:
            if candidate.IsTemplate or candidate.ViewType != ViewType.FloorPlan:
                continue
            wall_count = FilteredElementCollector(
                doc, candidate.Id).OfClass(Wall).GetElementCount()
            if wall_count > best_wall_count:
                best_wall_count = wall_count
                view = candidate
        except Exception:
            pass
    if view is None:
        view = uidoc.ActiveView
    out_dir = os.environ.get("REVIT_OUTPUT_DIR", "")
    if not out_dir:
        raise RuntimeError("请配置 REVIT_OUTPUT_DIR")
    result_path = os.path.join(out_dir, "cv_view_export_result.json")
    result = {"status": "error", "view": view.Name if view else ""}
    try:
        if view is None or view.IsTemplate or view.ViewType == ViewType.Internal:
            raise ValueError("active view cannot be exported")
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        prefix = os.path.join(out_dir, "cv_active_view")
        started = time.time()
        options = ImageExportOptions()
        options.ExportRange = ExportRange.SetOfViews
        view_ids = List[ElementId]()
        view_ids.Add(view.Id)
        options.SetViewsAndSheets(view_ids)
        options.FilePath = prefix
        options.HLRandWFViewsFileType = ImageFileType.PNG
        options.ShadowViewsFileType = ImageFileType.PNG
        options.ImageResolution = ImageResolution.DPI_300
        options.ZoomType = ZoomFitType.FitToPage
        options.PixelSize = 4096
        doc.ExportImage(options)
        candidates = []
        for name in os.listdir(out_dir):
            path = os.path.join(out_dir, name)
            if (name.lower().endswith(".png") and
                    name.lower().startswith("cv_active_view") and
                    os.path.getmtime(path) >= started - 2.0):
                candidates.append(path)
        if not candidates:
            raise ValueError("Revit did not produce the PNG")
        candidates.sort(key=os.path.getmtime, reverse=True)
        result.update({"status": "done", "path": candidates[0],
                       "pixel_size": 4096})
    except Exception as ex:
        result["message"] = str(ex)[:300]
    with open(result_path, "w") as stream:
        json.dump(result, stream, indent=1)
    print(json.dumps(result))
    return result


main()
