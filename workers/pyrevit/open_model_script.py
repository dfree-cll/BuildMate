import os
from Autodesk.Revit.UI import TaskDialog

MODEL_PATH = os.environ.get("REVIT_MODEL_PATH", "")

try:
    if not MODEL_PATH:
        raise ValueError("请配置 REVIT_MODEL_PATH")
    __revit__.OpenAndActivateDocument(MODEL_PATH)
except Exception as ex:
    TaskDialog.Show("BuildMate Open Model Error", str(ex)[:800])
