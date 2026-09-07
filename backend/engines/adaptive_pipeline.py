"""BuildMate 自适应建模管线。

之前的痛点: BuildMate 自己用旧 _generate 聚类 → 柱 44 个/坐标乱/质量差
本管线: 上传 DWG → 转 DXF → extract_columns_v2(自适应柱/轴网/标高, 318 柱验证)
        → merge_layers(多层合并) → model.json → json2rvt 建模

验证基准: SG20 114柱 + B1 204柱 = 318 柱, 坐标匹配 100%
"""
import json
import logging
import os
import re
import subprocess
import sys
import uuid

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_JSON_IN = os.environ.get("BIM_JSON_IN", os.path.join(
    _PROJECT_ROOT, "data", "runtime", "revit", "json_in"))

# 上传文件保存目录(BuildMate 自己的, 不碰 data/uploads 旧文件)
_UPLOAD = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "uploads_v2")
_ODA_FILE_CONVERTER = os.environ.get("ODA_FILE_CONVERTER", "")
_COLUMN_EXTRACTOR = os.path.join(
    _PROJECT_ROOT, "scripts", "bim_pipeline", "extract_columns_v2.py")
_DEFAULT_COLUMN_FAMILY = os.environ.get(
    "REVIT_COLUMN_FAMILY",
    r"C:\ProgramData\Autodesk\RVT 2020\Libraries\China\结构\柱\混凝土\混凝土 - 矩形 - 柱.rfa")
_DEFAULT_BEAM_FAMILY = os.environ.get(
    "REVIT_BEAM_FAMILY",
    r"C:\ProgramData\Autodesk\RVT 2020\Libraries\China\结构\框架\混凝土\混凝土 - 矩形梁.rfa")
logger = logging.getLogger(__name__)


def save_upload(file_bytes: bytes, filename: str) -> str:
    """保存上传文件(DWG 自动转 DXF), 返回 DXF 路径"""
    os.makedirs(_UPLOAD, exist_ok=True)
    ext = os.path.splitext(filename)[1].lower() or ".dxf"
    upload_id = "up_" + uuid.uuid4().hex[:10]
    raw_path = os.path.join(_UPLOAD, upload_id + ext)
    with open(raw_path, "wb") as f:
        f.write(file_bytes)
    if ext == ".dwg":
        if not os.path.isfile(_ODA_FILE_CONVERTER):
            raise RuntimeError(
                "ODA File Converter 未配置或文件不存在: %s" %
                _ODA_FILE_CONVERTER)
        # ODA converts directories.  Isolate every upload so unrelated DWGs
        # can never be swept into this request's output.
        stage = os.path.join(_UPLOAD, upload_id + "_oda")
        input_dir = os.path.join(stage, "input")
        output_dir = os.path.join(stage, "output")
        os.makedirs(input_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)
        staged_path = os.path.join(input_dir, upload_id + ".dwg")
        os.replace(raw_path, staged_path)
        r = subprocess.run([
            _ODA_FILE_CONVERTER, input_dir, output_dir,
            "ACAD2018", "DXF", "0", "1",
        ], capture_output=True, timeout=300)
        dxf_path = os.path.join(output_dir, upload_id + ".dxf")
        if r.returncode != 0 or not os.path.exists(dxf_path):
            raise RuntimeError("ODA DWG conversion failed: %s" %
                               (r.stderr or r.stdout or b"")[-1000:])
        return dxf_path
    return raw_path


def _run_extract(dxf_path: str) -> tuple[str, dict]:
    """跑 extract_columns_v2(验证过的自适应提取), 返回 (块名前缀, 提取结果)"""
    if not os.path.isfile(_COLUMN_EXTRACTOR):
        raise RuntimeError("项目内柱提取脚本不存在: %s" %
                           _COLUMN_EXTRACTOR)
    py = sys.executable
    os.makedirs(_JSON_IN, exist_ok=True)
    r = subprocess.run([py, _COLUMN_EXTRACTOR, dxf_path],
                       capture_output=True, timeout=600, text=True)
    if r.returncode != 0:
        raise RuntimeError("extract failed: %s" % (r.stderr or r.stdout)[-300:])
    out = r.stdout
    # 解析 [OK] 行拿输出前缀
    prefix = None
    for line in out.splitlines():
        if "[OK]" in line:
            prefix = line.split("输出")[-1].strip()
    if not prefix:
        raise RuntimeError("extract no output: %s" % out[-300:])
    grid_path = os.path.join(_JSON_IN, f"{prefix}_grid.json")
    cols_path = os.path.join(_JSON_IN, f"{prefix}_columns.json")
    grid = json.load(open(grid_path, encoding="utf-8"))
    cols = json.load(open(cols_path, encoding="utf-8"))
    return prefix, {"grid": grid, "cols": cols}


def build_model_json(dxf_paths: list[str], out_path: str = None) -> dict:
    """多层 DXF → 合并 model.json(每层独立标高/轴网, 复用 merge_layers 逻辑)

    dxf_paths: 每张图一个路径(可多层), 各层独立提取后合并
    """
    layers = []  # (层名, grid, cols)
    for dxf in dxf_paths:
        prefix, res = _run_extract(dxf)
        cols = res["cols"]
        if not cols:
            continue
        # 层名 = 块名主体(去掉括号标高), 如 'B1层墙柱(-6.5~-0.1m)' → 'B1层'
        import re
        m = re.match(r'(.+?)(?:层|墙柱)', prefix)
        lv = m.group(1) if m else prefix[:4]
        layers.append((lv, res["grid"], cols))
    if not layers:
        raise RuntimeError("无任何层可提取")

    # 合并(与 merge_layers.py 相同逻辑, 内联避免 subprocess)
    levels = []
    for lv, _, cols in layers:
        base = cols[0].get("elev_base_m", 0.0)
        top = cols[0].get("elev_top_m", 3.0)
        levels.append({"name": lv, "elevation": int(base * 1000)})
        levels.append({"name": lv + "顶", "elevation": int(top * 1000)})
    grids = [{"level": lv, "x_axes": [round(a["coord"] * 1000, 1) for a in g["x_axes"]],
              "y_axes": [round(a["coord"] * 1000, 1) for a in g["y_axes"]]}
             for lv, g, _ in layers]
    elems = []
    for lv, _, cols in layers:
        base = cols[0].get("elev_base_m", 0.0)
        top = cols[0].get("elev_top_m", 3.0)
        for i, c in enumerate(cols):
            elems.append({"type": "Column", "id": "%s_col_%d" % (lv, i),
                          "x": round(c["center"][0] * 1000), "y": round(c["center"][1] * 1000),
                          "width": round(c["size"][0] * 1000), "depth": round(c["size"][1] * 1000),
                          "base": 0, "top": round((top - base) * 1000), "level": lv})
    model = {"project": {"name": "+".join(l[0] for l in layers) + " 多层模型",
                         "levels": levels},
             "grids": grids, "model_elements": elems}
    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        json.dump(model, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return model


def build_from_upload(dxf_path: str, out_path: str = None) -> dict:
    """单张上传图 → model.json(一层或图内多层)"""
    return build_model_json([dxf_path], out_path)


def validate_revit_input(model: dict | None, status: dict | None,
                         require_architecture: bool = True,
                         coordinate_accuracy_min: float = 0.95,
                         geometry_accuracy_min: float = 0.98) -> list[str]:
    """Revit 前置质量门禁。

    这里校验当前执行器实际消费的毫米制 IR，而不是宽松接受文档中的另一套字段。
    返回空列表才允许触发 Revit。
    """
    errors = []
    if not isinstance(model, dict):
        return ["model.json 不存在或不是对象"]
    project = model.get("project") or {}
    if project.get("units") != "mm":
        errors.append("project.units 必须明确为 mm")
    project_id = project.get("project_id")
    build = model.get("build") or {}
    floor_code = str(build.get("floor_code") or "").upper()
    if not project_id:
        errors.append("project.project_id 必须明确")
    if not re.match(r"^(B[1-9]\d*|[1-9]\d*F|RF)$", floor_code):
        errors.append("build.floor_code 必须是 B1、1F 或 RF")
    if not project.get("levels"):
        errors.append("缺少 project.levels")
    elif floor_code and not any(
            level.get("name") == "标高 " + floor_code
            for level in project.get("levels") or []):
        errors.append("当前楼层未出现在 project.levels 中")
    if not (model.get("grids") or model.get("grid")):
        errors.append("缺少轴网 grids/grid")
    elements = model.get("model_elements") or []
    if not elements:
        errors.append("model_elements 为空")
    for i, elem in enumerate(elements):
        typ = elem.get("type")
        if floor_code and elem.get("level") != "标高 " + floor_code:
            errors.append(f"Element[{i}] 未绑定当前项目标高")
        if typ == "Wall" and not all(k in elem for k in ("start", "end", "thickness", "height", "level")):
            errors.append(f"Wall[{i}] 字段不完整")
        elif (typ == "Column" and elem.get("profile") and
              not all(k in elem for k in ("x", "y", "base", "top", "level"))):
            errors.append(f"Profile Column[{i}] 字段不完整")
        elif (typ == "Column" and not elem.get("profile") and
              not all(k in elem for k in ("x", "y", "width", "depth", "base", "top", "level"))):
            errors.append(f"Column[{i}] 字段不完整")
        elif typ == "Beam" and not all(k in elem for k in ("start", "end", "width", "height", "level")):
            errors.append(f"Beam[{i}] 字段不完整")
        elif typ in ("Column", "Beam") and not elem.get("profile") and not elem.get("type_name"):
            errors.append(f"{typ}[{i}] 缺少精确族类型 type_name")
        elif typ in ("Column", "Beam") and not elem.get("profile") and not os.path.isfile(elem.get("family_path") or ""):
            errors.append(f"{typ}[{i}] 族文件不存在")
        elif typ in ("Opening", "Stair"):
            errors.append(f"当前 Revit 执行器不支持 {typ}")
        if len(errors) >= 20:
            break
    gates = (status or {}).get("MODEL_STATUS") or {}
    for name in ("Step0_数据清洗", "Step1_轴网标高", "Step2_柱剪力墙",
                 "结构框架Gate"):
        if gates.get(name) != "PASS":
            errors.append(f"{name}={gates.get(name, 'MISSING')}")
    quality = (status or {}).get("数据质量") or {}
    coordinate_accuracy = quality.get("坐标准确率")
    if coordinate_accuracy is None or float(coordinate_accuracy) < coordinate_accuracy_min:
        errors.append(f"坐标准确率={coordinate_accuracy if coordinate_accuracy is not None else 'MISSING'}，需≥{coordinate_accuracy_min}")
    geometry_accuracy = quality.get("几何准确率")
    if geometry_accuracy is None or float(geometry_accuracy) < geometry_accuracy_min:
        errors.append(f"几何准确率={geometry_accuracy if geometry_accuracy is not None else 'MISSING'}，需≥{geometry_accuracy_min}")
    if require_architecture and gates.get("建筑空间Gate") != "PASS":
        errors.append(f"建筑空间Gate={gates.get('建筑空间Gate', 'MISSING')}")
    return errors


def _append_boundary_columns(model: dict, dxf_path: str) -> int:
    """Append verified GBZ/YBZ profiles using the model's coordinate/level contract."""
    from backend.engines.boundary_columns import extract_boundary_columns

    extracted = extract_boundary_columns(dxf_path)
    transform = (extracted.get("meta") or {}).get("transform") or {}
    elev_m = transform.get("elev_m")
    if not isinstance(elev_m, (list, tuple)) or len(elev_m) != 2:
        return 0
    base_mm, top_mm = (round(float(elev_m[0]) * 1000),
                       round(float(elev_m[1]) * 1000))
    levels = (model.get("project") or {}).get("levels") or []
    level = min(levels, key=lambda item: abs(float(item.get("elevation", 0)) - base_mm),
                default={}).get("name")
    if not level:
        return 0
    elements = model.setdefault("model_elements", [])
    existing_ids = {element.get("id") for element in elements}
    grids = model.get("grids") or []
    grid = grids[0] if isinstance(grids, list) and grids else model.get("grid") or {}
    x_axes = [float(value) for value in grid.get("x_axes") or []]
    y_axes = [float(value) for value in grid.get("y_axes") or []]
    margin_mm = 5000.0

    def in_floor_scope(column: dict) -> bool:
        if float(column.get("label_distance_mm", float("inf"))) > 2000.0:
            return False
        if not x_axes or not y_axes:
            return True
        return (min(x_axes) - margin_mm <= float(column["x"]) <= max(x_axes) + margin_mm and
                min(y_axes) - margin_mm <= float(column["y"]) <= max(y_axes) + margin_mm)

    def covered_rectangular_ids(column: dict) -> set[str]:
        points = (column.get("profile") or {}).get("points") or []
        if len(points) < 3:
            return set()
        xs = [float(column["x"]) + float(point[0]) for point in points]
        ys = [float(column["y"]) + float(point[1]) for point in points]
        tolerance_mm = 100.0
        return {
            str(element.get("id")) for element in elements
            if (element.get("type") == "Column" and not element.get("profile") and
                element.get("level") == level and
                min(xs) - tolerance_mm <= float(element.get("x", float("inf"))) <= max(xs) + tolerance_mm and
                min(ys) - tolerance_mm <= float(element.get("y", float("inf"))) <= max(ys) + tolerance_mm)
        }

    offset = (model.get("build") or {}).get("boundary_column_offset_mm") or [0, 0]
    if not isinstance(offset, (list, tuple)) or len(offset) != 2:
        offset = [0, 0]
    source_columns = []
    for column in extracted.get("columns") or []:
        item = dict(column)
        item["x"] = round(float(column["x"]) + float(offset[0]), 3)
        item["y"] = round(float(column["y"]) + float(offset[1]), 3)
        if offset != [0, 0]:
            item["placement_transform"] = {
                "kind": "drawing-to-main-plan-translation",
                "offset_mm": [float(offset[0]), float(offset[1])],
            }
        if in_floor_scope(item):
            source_columns.append(item)

    appended = 0
    for column in source_columns:
        if column.get("id") in existing_ids or not in_floor_scope(column):
            continue
        replaced_ids = covered_rectangular_ids(column)
        if replaced_ids:
            elements[:] = [element for element in elements
                           if str(element.get("id")) not in replaced_ids]
            existing_ids.difference_update(replaced_ids)
        item = dict(column)
        item.update({"base": base_mm, "top": top_mm, "level": level})
        elements.append(item)
        existing_ids.add(item.get("id"))
        appended += 1
    return appended


def _clip_polygon_to_rect(points: list[list[float]], xmin: float,
                          xmax: float, ymin: float,
                          ymax: float) -> list[list[float]]:
    """Sutherland-Hodgman clip for an arbitrary polygon and axis-aligned wall."""
    polygon = [[float(point[0]), float(point[1])] for point in points]

    def clip(vertices, inside, intersect):
        if not vertices:
            return []
        output = []
        previous = vertices[-1]
        previous_inside = inside(previous)
        for current in vertices:
            current_inside = inside(current)
            if current_inside:
                if not previous_inside:
                    output.append(intersect(previous, current))
                output.append(current)
            elif previous_inside:
                output.append(intersect(previous, current))
            previous = current
            previous_inside = current_inside
        return output

    def vertical(x_value):
        def intersect(a, b):
            ratio = ((x_value - a[0]) / (b[0] - a[0])
                     if b[0] != a[0] else 0.0)
            return [x_value, a[1] + ratio * (b[1] - a[1])]
        return intersect

    def horizontal(y_value):
        def intersect(a, b):
            ratio = ((y_value - a[1]) / (b[1] - a[1])
                     if b[1] != a[1] else 0.0)
            return [a[0] + ratio * (b[0] - a[0]), y_value]
        return intersect

    polygon = clip(polygon, lambda p: p[0] >= xmin, vertical(xmin))
    polygon = clip(polygon, lambda p: p[0] <= xmax, vertical(xmax))
    polygon = clip(polygon, lambda p: p[1] >= ymin, horizontal(ymin))
    return clip(polygon, lambda p: p[1] <= ymax, horizontal(ymax))


def _polygon_area(points: list[list[float]]) -> float:
    return abs(sum(points[index][0] * points[(index + 1) % len(points)][1] -
                   points[(index + 1) % len(points)][0] * points[index][1]
                   for index in range(len(points)))) / 2.0 if len(points) >= 3 else 0.0


def _annotate_boundary_column_wall_targets(model: dict) -> int:
    """Persist exact source-level column/wall overlap pairs for Revit joining."""
    elements = model.get("model_elements") or []
    walls = [item for item in elements if item.get("type") == "Wall"]
    columns = [item for item in elements
               if item.get("type") == "Column" and item.get("profile")]
    pair_count = 0
    for column in columns:
        profile = (column.get("profile") or {}).get("points") or []
        world = [[float(column.get("x", 0)) + float(point[0]),
                  float(column.get("y", 0)) + float(point[1])]
                 for point in profile]
        targets = []
        for wall in walls:
            if wall.get("level") != column.get("level"):
                continue
            start = wall.get("start") or []
            end = wall.get("end") or []
            if len(start) < 2 or len(end) < 2:
                continue
            dx = abs(float(end[0]) - float(start[0]))
            dy = abs(float(end[1]) - float(start[1]))
            half = float(wall.get("thickness", 0)) / 2.0
            if dx >= dy and dy <= 2.0:
                xmin, xmax = sorted((float(start[0]), float(end[0])))
                center = (float(start[1]) + float(end[1])) / 2.0
                ymin, ymax = center - half, center + half
            elif dy > dx and dx <= 2.0:
                ymin, ymax = sorted((float(start[1]), float(end[1])))
                center = (float(start[0]) + float(end[0])) / 2.0
                xmin, xmax = center - half, center + half
            else:
                continue
            clipped = _clip_polygon_to_rect(world, xmin, xmax, ymin, ymax)
            if _polygon_area(clipped) > 1.0:
                targets.append(str(wall.get("id")))
        column["cut_wall_ids"] = targets
        pair_count += len(targets)
    return pair_count


def run_standard_pipeline(dxf_paths: list[str], build_id: str = "",
                          review_id: str = "", profile_config: dict | None = None) -> dict:
    """标准流程全链路(第十一期集成) —— 替换内联 build_model_json

    内联版只有柱(extract_columns_v2 + 手工合并), 无剪力墙/梁/墙/板, 无 Gate/无状态;
    项目内标准管线实现 Step0~7、双 Gate 与 MODEL_STATUS，
      全构件 mm 口径 model.json(B1 实测: 204柱/32剪力墙/358梁; 多层合并 318柱/44墙/758梁),
      此处 subprocess 调用, 后端拿到 model.json + model_status.json 双产物。

    返回: {"model": model.json dict, "status": model_status dict, "log": 尾部日志}
    """
    py = sys.executable
    runner = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "scripts", "pipeline_std_runner.py")
    cmd = [py, runner] + list(dxf_paths) + ["--skip-build"]
    r = subprocess.run(cmd, capture_output=True, timeout=1800, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError("标准流程失败: %s" % (r.stderr or r.stdout)[-300:])
    model_path = os.path.join(_JSON_IN, "model.json")
    status_path = os.path.join(_JSON_IN, "model_status.json")
    model = json.load(open(model_path, encoding="utf-8")) if os.path.exists(model_path) else None
    status = json.load(open(status_path, encoding="utf-8")) if os.path.exists(status_path) else None
    if isinstance(model, dict):
        profile_config = profile_config or {}
        model.setdefault("schema_version", "2.0")
        project = model.setdefault("project", {})
        project["units"] = "mm"
        gates = (status or {}).get("MODEL_STATUS") or {}
        project["model_scope"] = ("structural" if gates.get("建筑空间Gate") == "N/A"
                                  else "full")
        model["build"] = {"build_id": build_id, "review_id": review_id,
                          "source_files": [os.path.abspath(p) for p in dxf_paths]}
        if profile_config:
            model["build"]["drawing_profile_applied"] = True
        # GBZ/YBZ use a hatch outline rather than a rectangular column block.
        # Keep that source geometry in the same model.json consumed by Revit;
        # otherwise the automatic path silently omits the irregular columns.
        try:
            boundary_count = _append_boundary_columns(model, dxf_paths[0])
            if boundary_count:
                model["build"]["boundary_column_count"] = boundary_count
                model["build"]["boundary_column_wall_pair_count"] = \
                    _annotate_boundary_column_wall_targets(model)
        except Exception as ex:
            logger.warning("boundary_column_extract_failed: %s", str(ex)[:160])
        # 精确结构族契约：按截面生成稳定类型名，并显式携带族库路径。
        # Revit 端只在路径存在且尺寸参数可设置时创建真实 FamilyInstance。
        for elem in model.get("model_elements") or []:
            typ = elem.get("type")
            if typ == "Column" and not elem.get("profile"):
                w, d = round(float(elem.get("width", 0))), round(float(elem.get("depth", 0)))
                elem.setdefault("type_name", f"BM_ConcreteColumn_{w}x{d}")
                elem.setdefault("family_path", profile_config.get(
                    "column_family_path", _DEFAULT_COLUMN_FAMILY))
            elif typ == "Beam":
                w, h = round(float(elem.get("width", 0))), round(float(elem.get("height", 0)))
                elem.setdefault("type_name", f"BM_ConcreteBeam_{w}x{h}")
                elem.setdefault("family_path", profile_config.get(
                    "beam_family_path", _DEFAULT_BEAM_FAMILY))
        # Revit 端读取固定文件；在触发前把归一化后的唯一契约写回。
        with open(model_path, "w", encoding="utf-8") as f:
            json.dump(model, f, ensure_ascii=False, indent=1)
    return {"model": model, "status": status, "log": (r.stdout or "")[-2000:]}
