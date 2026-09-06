# BuildMate BIM 国标建模标准（可执行配置）

本标准在当前产品 中继续作为 BIM 独立入口的执行基线；问答入口只能跳转/预填 BIM 任务，不能绕过本标准的门禁、审批和独立叠图验收。

## 1. 目的与适用范围

本文件定义 BuildMate BIM Agent 的默认“国标建模”配置，适用于 `PDF/DWG/DXF → WallEvidence → WallModel → Revit 2020 → 独立叠图验收` 主链路。

这里的“按国标”首先体现在模型生成和交付字段：系统按统一单位/坐标、分类编码、命名、楼层、构件参数、数量和来源证据生成 WallModel 与 Revit 实例信息，并把 profile 写入交付物。它不是对建筑设计、结构设计或消防规范符合性的法律认证。项目的结构、抗震、防火等专业规范仍由设计人员和审核人员确认。

## 2. 采用的国家标准

| 标准 | 在 BuildMate 中的定位 | 当前可执行内容 |
| --- | --- | --- |
| GB/T 51212-2016《建筑信息模型应用统一标准》 | BIM 应用与信息组织总框架 | 统一单位、坐标、模型/证据可追溯和跨阶段交接 |
| GB/T 51269-2017《建筑信息模型分类和编码标准》 | 分类与编码 | 墙、柱等构件必须有稳定分类编码和可追溯类型标识 |
| GB/T 51301-2018《建筑信息模型设计交付标准》 | 设计交付 | 交付模型必须包含楼层、类型、材料（若项目要求）、几何数量和来源引用 |
| GB/T 51235-2017《建筑信息模型施工应用标准》 | 施工阶段应用（启用施工交付时） | 保留施工应用所需的版本、任务和审批链路 |

国家标准的公开状态和范围以标准发布平台为准：[GB/T 51212-2016](https://ebook.chinabuilding.com.cn/zbooklib/book/detail/show?SiteID=1&bookID=73539)、[GB/T 51269-2017](https://ebook.chinabuilding.com.cn/zbooklib/book/detail/show?SiteID=1&bookID=102672)、[GB/T 51235-2017](https://www.ndls.org.cn/standard/detail/29935894d6882623a7d273fab53abe88)、[GB/T 51301-2018](https://www.gb-gbt.com/PDF/Chinese.aspx/GBT51301-2018)。

2025 年发布的 GB/T 45393.1—5 系列属于 BIM 软件能力、参数化模型、MVD 和数据接口等软件层标准，可作为后续软件测评参考；本项目不以它替代上述模型应用和交付标准。

## 3. 默认标准配置

配置位置：`config/wall_pipeline.example.yaml`；当前 API 可以通过 `options.modeling_standard` 覆盖项目级开关，但不得改变租户、任务和来源文件的边界。

```yaml
modeling_standard:
  profile: cn_gb_bim_delivery_v1
  references:
    - GB/T 51212-2016
    - GB/T 51269-2017
    - GB/T 51301-2018
    - GB/T 51235-2017
  # 引擎内部几何单位固定为 m；Revit 交付与界面显示统一为 mm
  units: m
  delivery_units: mm
  coordinate_frame: project_north
  classification_system: GB/T 51269-2017
  application_standard: GB/T 51212-2016
  delivery_standard: GB/T 51301-2018
  construction_standard: GB/T 51235-2017
  require_source_refs: true
  require_classification_code: true
  require_instance_parameters: true
  require_level: true
  require_material: false
  require_quantities: true
```

`require_material` 默认为 `false`，因为很多图纸只给出构件类别而没有可靠的材料牌号；项目可以在材料信息已确认时改为 `true`，此时缺失材料会阻断交付，不会猜测。

## 4. 可执行规则

### 4.1 坐标、单位和轴网

1. WallEvidence、WallModel 的几何单位固定为米（`m`），这是内部稳定的规范化坐标单位；Revit 交付 DTO、构件工程参数和项目显示单位固定为毫米（`mm`）。用户可见的工程长度不得以英寸显示。
2. 必须记录 `coordinate_origin` 和完整 `transform_chain`，包括来源单位、旋转、平移和轴网校正。
3. 轴网先于墙柱建模；启用 `grid.required` 时，缺少有效横纵轴网会阻断审核门禁。
4. PDF/DWG 只在来源适配层分叉，下游合同和标准校验不按图号、文件名或项目特例分叉。

### 4.2 分类、命名和版本

1. 墙、柱使用 `Wall` / `Column` 类别，并携带 `classification_code`（默认 `IfcWall` / `IfcColumn`，项目可在配置中指定）。
2. `wall_id`、`column_id` 在一个 WallModel 内全局唯一，作为 Revit 回读和工程量追踪键。
3. `family_name`、`family_type`、`type_name`、`type_mark`、楼层和标准 profile 进入构件实例信息；不能只写在说明文档中。
4. 墙、柱、梁统一按“图纸编号-整数毫米规格”命名，例如 `Q1-500mm`、`KZ1-600x500mm`、`LL1-300x700mm`；原图编号保留在 `BM_TypeMark`。尺寸必须来自图例/构件表/大样；未核定时类型名使用 `*-规格待核定-稳定构件ID`，避免未知截面共用同一类型，不得四舍五入实测小数充当图纸规格。矩形柱在几何容差校验通过后同步正式截面；异形柱不得用局部肢长缩放整体轮廓。
5. 同一来源文件哈希、解析器版本、分块/模型版本和标准配置参与产物身份计算，变更后必须产生新版本。

### 4.3 证据和几何可信度

1. 每面墙、每根柱至少有一个 `source_refs`，引用来源文件、实体、图页/定位信息。
2. OCR、YOLO、颜色和 LLM 只能作为语义或分类辅助，不能直接决定墙柱坐标。
3. 墙厚、长度、连接关系、柱轮廓和碰撞由 NumPy/Shapely 等确定性代码计算。
4. 无法建立来源证据、几何无效、构件重复或墙柱碰撞时，门禁失败并进入审核/修复，不得静默生成假构件。
5. 图例、构件表和大样是构件规格的权威来源；解析结果必须保存原文、来源引用和状态。几何仅用于校验，规格与实测值冲突时标记 `conflict` 并人工复核。

### 4.4 构件信息和工程量

每个 Wall/Column 必须（按 profile 开关）携带：

- 族名、族类型、类型名和分类编码；
- 楼层、标高和高度；
- 材料及材料状态（已提供/未注明）；
- 来源引用和识别置信度；
- 基于确定性几何的长度、截面/占地、侧面积和体积。

这些字段会编译为 Revit 2020 可写入的 `BM_*` 实例参数，供后续算量、回读和差异审计使用。矩形柱优先解析为目标模型中的真实结构柱族；只有异形轮廓或目标模型没有可匹配族时，才使用明确标记的精确轮廓 DirectShape，不伪装成族实例。

### 4.5 审核、写入和交付

1. 标准 profile 参与 WallModel 生成和 Revit 参数编译；标准字段缺失会记录为可读的模型元数据问题，不用“标准门禁”替代几何安全审核。
2. Revit 写入顺序为：静态几何校验 → Dry-run → 持久化人工批准 → Windows Bridge Transaction。
3. 写入失败必须回滚或明确失败；交付验收使用独立渲染的原始图纸和 Revit 实际视图，禁止用 WallModel 反绘原图自证。
4. 验收报告必须保留标准 profile、来源哈希、Revit 回读和独立叠图指标。

## 5. 代码映射

| 规则 | 实现位置 |
| --- | --- |
| 标准配置与强制引用 | `backend/engines/wall_pipeline/contracts.py` 的 `ModelingStandardConfig` |
| 标准字段与模型元数据检查 | `backend/engines/wall_pipeline/standards.py` |
| 几何门禁汇总 | `backend/engines/wall_pipeline/geometry.py` 的 `build_wall_model` |
| 配置/API 覆盖和租户边界 | `backend/application/wall_pipeline_workflow.py` |
| 产物血缘一致性 | `backend/engines/wall_pipeline/pipeline.py` |
| Revit 2020 参数编译与族解析 | `workers/revit_bridge/wall_compiler.py` |
| 前端标准提示 | `frontend/src/views/BimReviewView.vue` |

## 6. 不在本 profile 自动推断的内容

- 结构抗震等级、构造要求、耐火极限等专业规范结论；
- 项目专属族库、企业编码表、LOD/LOI 详细交付矩阵；
- 材料牌号、强度等级和施工工艺（图纸没有可靠证据时）；
- 未具备完整尺寸/标高/宿主证据的洞口不会自动开洞；完整证据的洞口仍须通过静态校验、Dry-run 和人工审批。

以上信息必须作为项目配置、已批准知识文档或人工审批输入，不能由几何算法或模型自行编造。

## 7. 验收要求

标准相关自动化测试至少覆盖：默认 profile、缺失国标引用、单位/坐标、重复 ID、缺失来源证据、缺失构件参数、缺失工程量，以及标准元数据进入 Revit 编译物。最终交付仍必须通过原有几何门禁、Revit 审批流程和独立叠图验收。
