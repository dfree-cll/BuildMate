# -*- coding: utf-8 -*-
"""IFC → RVT 批量转换脚本（pyRevit 插件运行，兼容 Revit 2018-2025）

使用方法：
1. 安装免费插件 pyRevit（https://github.com/eirannejad/pyRevit/releases，
   选与你的 Revit 版本匹配的安装包；Revit 2020 对应 pyRevit 4.12+）
2. 安装后 Revit 顶部出现 pyRevit 选项卡 → pyRevit → Scripts 目录（右键打开）
3. 把本文件（ifc2rvt_batch.py）复制进 Scripts 目录
4. 配置 IFC_INPUT_DIR 和 REVIT_OUTPUT_DIR 两个环境变量
5. 把要转换的 IFC 放入 ifc_in → 回到 Revit → pyRevit 选项卡 → 点脚本名运行

注意：请在打开任意 Revit 项目（新项目即可）后运行本脚本。
"""
import os
import clr

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
from Autodesk.Revit.DB import IFCImportOptions
from Autodesk.Revit.UI import TaskDialog

IFC_IN = os.environ.get("IFC_INPUT_DIR", "")
RVT_OUT = os.environ.get("REVIT_OUTPUT_DIR", "")

doc = __revit__.ActiveUIDocument.Document
app = doc.Application

if not IFC_IN or not os.path.isdir(IFC_IN):
    TaskDialog.Show("提示", "文件夹不存在，请创建: " + IFC_IN)
    raise SystemExit

if not RVT_OUT:
    TaskDialog.Show("提示", "请配置 REVIT_OUTPUT_DIR")
    raise SystemExit
os.makedirs(RVT_OUT, exist_ok=True)

ok, fail = [], []
for name in sorted(os.listdir(IFC_IN)):
    if not name.lower().endswith(".ifc"):
        continue
    src = os.path.join(IFC_IN, name)
    out = os.path.join(RVT_OUT, os.path.splitext(name)[0] + ".rvt")
    try:
        # 打开 IFC：优先原生 IFC 打开（Revit 2019+），失败回退通用打开（2018）
        ifc_doc = None
        try:
            ifc_doc = app.OpenIFCDocument(src)
        except Exception:
            ifc_doc = app.OpenDocumentFile(src)
        # 另存为 RVT（2018-2025 通用）
        ifc_doc.SaveAs(out)
        ifc_doc.Close(False)
        ok.append(name)
    except Exception as e:
        fail.append(name + "：" + str(e)[:80])

TaskDialog.Show("IFC→RVT 转换完成",
    "成功 {} 个：\n{}\n\n失败 {} 个：\n{}\n输出目录：{}".format(
        len(ok), "\n".join(ok) or "无", len(fail), "\n".join(fail) or "无", RVT_OUT))
